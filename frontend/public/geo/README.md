# India state boundaries (map layer)

`india-states.geojson` is a web-optimised state/UT boundary layer for the
Overview choropleth.

- **Source:** DataMeet India community boundary set, via the simplified
  [GeoDataViewer India States & Union Territories dataset](https://geodataviewer.com/datasets/boundaries/india-states-boundaries/).
- **Coverage:** 36 states and union territories (28 states + 8 UTs), including
  Telangana and Ladakh as separate units.
- **Processing:** properties reduced to `name` + ISO 3166-2 `code`
  (`shapeISO`); coordinates rounded to 4 decimal places; consecutive duplicate
  vertices removed; rings explicitly closed; exterior rings rewound clockwise
  for d3-geo's spherical polygon convention (otherwise D3 fills the complement
  of each state). No boundaries were hand-drawn or approximated beyond the
  source's published ~1 km simplification.
- **License:** CC BY 2.5 India (DataMeet). Attribution is shown in the map
  section footer.

The application joins database aggregates to features by `code`, never by
display name. `backend/state_geo.py` is the single name-normalisation table
between recorded state labels and these ISO codes.
