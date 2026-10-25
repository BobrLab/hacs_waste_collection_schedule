import re
import unicodedata
from collections import defaultdict
from datetime import date
from io import BytesIO
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from waste_collection_schedule import Collection, Icons
from waste_collection_schedule.exceptions import (
    SourceArgumentNotFound,
    SourceArgumentRequired,
)

TITLE = "Nemėžio komunalininkas"
DESCRIPTION = "Waste collection schedules for Vilnius District, Lithuania."
URL = "https://www.nkom.lt/kita.html"
COUNTRY = "lt"

TEST_CASES = {
    "Nemėžis": {"location": "Nemėžis"},
}

PARAM_TRANSLATIONS = {
    "en": {"location": "Location or street"},
}

PARAM_DESCRIPTIONS = {
    "en": {
        "location": "A location or street name as listed in the schedule's 'Vietovė' column.",
    },
}

HOW_TO_GET_ARGUMENTS_DESCRIPTION = {
    "en": "Open https://www.nkom.lt/kita.html and use a town, village, or street name as it appears in the 'Vietovė' column of the schedule.",
}

ICON_MAP = {
    "mixed": Icons.GENERAL_WASTE,
    "packaging": Icons.PLASTIC_PACKAGING,
    "glass": Icons.GLASS,
}

_PAGE_URL = URL
_MONTHS = {
    "sausis": 1,
    "vasaris": 2,
    "kovas": 3,
    "balandis": 4,
    "geguze": 5,
    "birzelis": 6,
    "liepa": 7,
    "rugpjutis": 8,
    "rugsejis": 9,
    "spalis": 10,
    "lapkritis": 11,
    "gruodis": 12,
}
_ROUTE_RE = re.compile(r"\b[A-Z]{3}\s*\d{3}(?:\s*\(\d+\))?\b")
_DAY_RE = re.compile(r"\d{1,2}")
_DATE_LIST_RE = re.compile(r"\d{1,2}(?:\s*,\s*\d{1,2})*")
_MAX_PDF_BYTES = 5 * 1024 * 1024
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
}


def _fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value).casefold()
        if not unicodedata.combining(character)
    )


def _schedule_kind(href: str) -> str | None:
    filename = _fold(unquote(urlparse(href).path))
    if not filename.endswith(".pdf"):
        return None
    if "grafikas" not in filename:
        return None
    if "pakuociu" in filename:
        return "packaging"
    if "misriu" in filename:
        return "mixed"
    return None


def _schedule_period(href: str) -> tuple[int, int]:
    decoded = unquote(href)
    match = re.search(r"\b(20\d{2})\b.*?\((\d{1,2})-(\d{1,2})\)", decoded)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(3)))


def _discover_pdf_urls(session: requests.Session) -> dict[str, str]:
    response = session.get(_PAGE_URL, timeout=30, headers=_HEADERS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    schedules: dict[str, tuple[tuple[int, int], str]] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(_PAGE_URL, str(anchor["href"]))
        kind = _schedule_kind(href)
        if kind is None:
            continue
        period = _schedule_period(href)
        if kind not in schedules or period > schedules[kind][0]:
            schedules[kind] = (period, href)

    missing = {"mixed", "packaging"} - schedules.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Could not find current NKOM PDF schedule(s): {names}")
    return {kind: schedule[1] for kind, schedule in schedules.items()}


def _extract_spans(page) -> list[tuple[float, float, float, str]]:
    height = float(page.mediabox.height)
    spans: list[tuple[float, float, float, str]] = []

    def visitor(text, _cm, text_matrix, _font_dict, font_size):
        a, b, c, d, e, f = _cm
        x = a * text_matrix[4] + c * text_matrix[5] + e
        y = height - (b * text_matrix[4] + d * text_matrix[5] + f)
        for line_number, line in enumerate(text.splitlines()):
            if line.strip():
                spans.append(
                    (
                        x,
                        y + line_number * float(font_size),
                        float(font_size),
                        line.strip(),
                    )
                )

    page.extract_text(visitor_text=visitor)
    return spans


def _header_layout(
    spans: list[tuple[float, float, float, str]],
) -> tuple[list[tuple[int, float]], float, float, float] | None:
    headers: dict[str, float] = {}
    months: list[tuple[int, float]] = []
    for x, y, _font_size, text in spans:
        if y > 190:
            continue
        normalized = _fold(text.strip(" :"))
        if normalized == "seniunija":
            headers["district"] = x
        elif normalized == "vietove":
            headers["location"] = x
        elif normalized.startswith("sav."):
            headers["weekday"] = x
        elif normalized in _MONTHS:
            months.append((_MONTHS[normalized], x))

    months.sort(key=lambda month: month[1])
    if (
        "district" not in headers
        or "location" not in headers
        or len(months) not in (3, 6)
    ):
        return None

    date_column_x = [x for _, x in months]
    locality_start = 120.0 if len(months) == 3 else 100.0
    locality_end = headers.get("weekday", date_column_x[0]) - 15.0
    return (
        months,
        headers["district"],
        locality_start,
        locality_end,
    )


def _group_lines(
    spans: list[tuple[float, float, float, str]],
) -> list[tuple[float, str]]:
    lines: list[list[tuple[float, float, float, str]]] = []
    for span in sorted(spans, key=lambda item: (item[1], item[0])):
        if not lines or abs(span[1] - lines[-1][0][1]) > 2.0:
            lines.append([span])
        else:
            lines[-1].append(span)

    return [
        (
            sum(span[1] for span in line) / len(line),
            " ".join(span[3] for span in sorted(line, key=lambda item: item[0])),
        )
        for line in lines
    ]


def _parse_pdf(content: bytes, kind: str, location: str) -> list[Collection]:
    if len(content) > _MAX_PDF_BYTES:
        raise ValueError(f"NKOM {kind} schedule PDF exceeds {_MAX_PDF_BYTES} bytes")

    reader = PdfReader(BytesIO(content))
    if not reader.pages:
        raise ValueError(f"NKOM {kind} schedule PDF has no pages")

    year_match = re.search(r"\b(20\d{2})\b", reader.pages[0].extract_text() or "")
    if year_match is None:
        raise ValueError(f"Could not determine the year in the NKOM {kind} schedule")
    year = int(year_match.group(1))

    folded_location = _fold(location)
    collections: list[Collection] = []
    header_layout = None
    found_rows = False
    matched_rows = False

    for page in reader.pages:
        spans = _extract_spans(page)
        page_layout = _header_layout(spans)
        if page_layout is not None:
            header_layout = page_layout
        if header_layout is None:
            continue
        month_columns, district_x, locality_start, locality_end = header_layout

        lines = _group_lines(spans)
        route_lines = [(y, text) for y, text in lines if _ROUTE_RE.search(text)]
        if not route_lines:
            continue
        found_rows = True

        page_height = float(page.mediabox.height)
        for index, (row_y, _row_text) in enumerate(route_lines):
            previous_y = route_lines[index - 1][0] if index else 0.0
            next_y = (
                route_lines[index + 1][0]
                if index + 1 < len(route_lines)
                else page_height
            )
            location_top = (previous_y + row_y) / 2
            location_bottom = (row_y + next_y) / 2
            if index == 0:
                location_top = max(location_top, row_y - 25.0)

            row_spans = [span for span in spans if abs(span[1] - row_y) <= 2.0]
            district = " ".join(
                span[3]
                for span in sorted(
                    (
                        span
                        for span in row_spans
                        if span[0] < locality_start and abs(span[0] - district_x) < 100
                    ),
                    key=lambda item: item[0],
                )
            )
            locality_parts = [
                span
                for span in spans
                if locality_start <= span[0] < locality_end
                and location_top <= span[1] < location_bottom
            ]
            row_location = " ".join(
                span[3]
                for span in sorted(locality_parts, key=lambda item: (item[1], item[0]))
            )
            if folded_location not in _fold(f"{district} {row_location}"):
                continue
            matched_rows = True

            row_values: dict[int, list[str]] = defaultdict(list)
            for span in row_spans:
                if not _DATE_LIST_RE.fullmatch(span[3]):
                    continue
                column_index = min(
                    range(len(month_columns)),
                    key=lambda month_index: abs(
                        span[0] - month_columns[month_index][1]
                    ),
                )
                if abs(span[0] - month_columns[column_index][1]) < 35:
                    row_values[column_index].append(span[3])

            for column_index, (month, _x) in enumerate(month_columns):
                waste_type = (
                    "Buitinės mišrios atliekos"
                    if kind == "mixed"
                    else "Pakuočių atliekos"
                    if column_index < 3
                    else "Stiklo pakuotės"
                )
                icon_key = (
                    "mixed"
                    if kind == "mixed"
                    else "packaging"
                    if column_index < 3
                    else "glass"
                )
                for value in row_values.get(column_index, []):
                    for day_text in _DAY_RE.findall(value):
                        day = int(day_text)
                        try:
                            event_date = date(year, month, day)
                        except ValueError as exc:
                            raise ValueError(
                                f"Invalid date in the NKOM {kind} schedule: "
                                f"{year}-{month:02d}-{day:02d}"
                            ) from exc
                        collections.append(
                            Collection(
                                event_date,
                                waste_type,
                                icon=ICON_MAP[icon_key],
                            )
                        )

    if header_layout is None:
        raise ValueError(f"Could not find the schedule table in the NKOM {kind} PDF")
    if not found_rows:
        raise ValueError(f"Could not find schedule rows in the NKOM {kind} PDF")
    if matched_rows and not collections:
        raise ValueError(
            f"No collection dates found for '{location}' in the NKOM {kind} PDF"
        )
    return collections


class Source:
    def __init__(self, location: str | None = None) -> None:
        if location is None or not location.strip():
            raise SourceArgumentRequired(
                "location",
                "Specify a village, town, or street name from the schedule.",
            )
        self._location = location.strip()

    def fetch(self) -> list[Collection]:
        session = requests.Session()
        schedules = _discover_pdf_urls(session)
        collections: list[Collection] = []

        for kind, url in schedules.items():
            response = session.get(url, timeout=30, headers=_HEADERS)
            response.raise_for_status()
            collections.extend(_parse_pdf(response.content, kind, self._location))

        if not collections:
            raise SourceArgumentNotFound("location", self._location)

        unique_collections = {
            (collection.date, collection.type): collection for collection in collections
        }
        return sorted(
            unique_collections.values(),
            key=lambda collection: (collection.date, collection.type),
        )
