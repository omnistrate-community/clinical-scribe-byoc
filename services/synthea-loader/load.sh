#!/usr/bin/env bash
# Generates synthetic patients with Synthea and loads them into a FHIR R4 server.
# All configuration comes from environment variables set by the synthea-loader chart.
set -euo pipefail

FHIR_BASE_URL="${FHIR_BASE_URL:?FHIR_BASE_URL is required}"
PATIENT_COUNT="${PATIENT_COUNT:-25}"
SEED="${SEED:-42}"
STATE="${STATE:-Massachusetts}"
AGE_RANGE="${AGE_RANGE:-35-85}"
SKIP_IF_PATIENTS_EXIST="${SKIP_IF_PATIENTS_EXIST:-true}"
OUT_DIR=/work/output

echo "Waiting for FHIR server at ${FHIR_BASE_URL}"
until curl -fsS "${FHIR_BASE_URL}/metadata" -o /dev/null; do sleep 5; done

if [[ "${SKIP_IF_PATIENTS_EXIST}" == "true" ]]; then
  existing=$(curl -fsS "${FHIR_BASE_URL}/Patient?_summary=count" | sed -n 's/.*"total"[^0-9]*\([0-9]*\).*/\1/p')
  if [[ "${existing:-0}" -gt 0 ]]; then
    echo "FHIR server already has ${existing} patients; skipping load"
    exit 0
  fi
fi

echo "Generating ${PATIENT_COUNT} patients (seed ${SEED}, ages ${AGE_RANGE}, ${STATE})"
java -jar /opt/synthea/synthea.jar \
  -p "${PATIENT_COUNT}" -s "${SEED}" -cs "${SEED}" -a "${AGE_RANGE}" \
  --exporter.baseDirectory "${OUT_DIR}/" \
  --exporter.fhir.export true \
  --exporter.csv.export false \
  --exporter.ccda.export false \
  --exporter.hospital.fhir.export true \
  --exporter.practitioner.fhir.export true \
  "${STATE}"

post_bundle() {
  local file="$1"
  local code
  code=$(curl -sS -o /tmp/resp.json -w '%{http_code}' -X POST "${FHIR_BASE_URL}" \
    -H 'Content-Type: application/fhir+json' --data-binary "@${file}")
  if [[ "${code}" != 2* ]]; then
    echo "Failed to load $(basename "${file}") (HTTP ${code})"
    head -c 1000 /tmp/resp.json; echo
    return 1
  fi
  echo "Loaded $(basename "${file}")"
}

shopt -s nullglob
# Organizations and practitioners must exist before patient bundles reference them.
for f in "${OUT_DIR}"/fhir/hospitalInformation*.json "${OUT_DIR}"/fhir/practitionerInformation*.json; do
  post_bundle "${f}"
done
for f in "${OUT_DIR}"/fhir/*.json; do
  case "$(basename "${f}")" in
    hospitalInformation*|practitionerInformation*) continue ;;
  esac
  post_bundle "${f}"
done
echo "Synthetic data load complete"
