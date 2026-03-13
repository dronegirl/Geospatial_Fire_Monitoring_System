# Geospatial_Fire_Monitoring_System
A geospatial wildfire early warning system integrating Meteosat MTG and VIIRS satellite detections to monitor fire risk near plantation forestry compartments. The system performs automated fire detection, risk ranking, dashboard visualisation, and SMS alerting for rapid response.
# SAA Forest Fire Early Warning System

**Developed by:** Ruvimbo Doreen Supiya  
**Institution:** University of Zimbabwe  
**Programme:** MSc Geomatics Engineering  
**Year:** 2026  

## Overview
This project is a geospatial early warning system for wildfire monitoring in plantation forestry.  
It integrates:

- Meteosat Third Generation (MTG) active fire detections for rapid regional monitoring
- VIIRS / FIRMS hotspots for more precise fire confirmation
- geospatial analysis of fire proximity to plantation compartments
- risk ranking based on distance to compartments
- dashboard monitoring
- forester alerting via SMS gateway integration

## Objectives
The main objective is to support rapid wildfire detection and response for plantation forestry, especially for compartment-based monitoring under the Sustainable Afforestation Association (SAA).

## Key Features
- Near-real-time satellite fire detection
- Southern Africa and Zimbabwe filtering
- Compartment proximity analysis
- Risk classification:
  - High risk: within 2 km
  - Medium risk: within 2–5 km
  - Low risk: within 5–10 km
- Export to GeoJSON for mapping in QGIS
- Ranked fire alert generation
- Dashboard support
- Forester notifications

## Workflow
1. Fetch Meteosat MTG active fire detections
2. Fetch VIIRS/FIRMS hotspot detections
3. Filter detections to the region of interest
4. Calculate distance to nearest plantation compartment
5. Assign fire risk level
6. Export monitoring layers and alert tables
7. Send alerts to forestry personnel

## Output Files
- `mtg_southern_africa.geojson`
- `viirs_southern_africa.geojson`
- `mtg_zimbabwe.geojson`
- `viirs_zimbabwe.geojson`
- `mtg_risk_alerts.geojson`
- `viirs_risk_alerts.geojson`
- `ranked_fire_alerts.csv`

## Author Statement
This work was designed and implemented by **Ruvimbo Doreen Supiya** as part of geospatial fire monitoring and wildfire early warning system development.  
Any reuse, adaptation, or academic submission must acknowledge the original author.

## License
See `LICENSE.txt`.
