# Nemėžio komunalininkas

Support for household mixed-waste and packaging collection schedules published by [Nemėžio komunalininkas](https://www.nkom.lt).

## Configuration via configuration.yaml

```yaml
waste_collection_schedule:
  sources:
    - name: nkom_lt
      args:
        location: "Nemėžis"
```

### Configuration Variables

**location**  
*(String) (required)* A town, village, or street name as it appears in the schedule's **Vietovė** column. A matching substring is sufficient.

## How to get the source arguments

1. Open [the NKOM collection schedules page](https://www.nkom.lt/kita.html).
2. Find your town, village, or street in the **Vietovė** column of the mixed-waste or packaging schedule.
3. Use that name, or a distinctive part of it, as `location`.

The source automatically discovers the latest mixed-waste and packaging PDF schedules on the page. The source returns mixed-waste, packaging, and glass-packaging events listed for the matching location.

The source uses the dated, individual-location schedules; it does not interpret the separate weekly table for shared apartment-container sites.
