# Blinded clinical validation app

Run locally with:

`streamlit run app.py`

Before deployment, make sure the parent analysis script has generated:

- `data/snippet_items_blinded.csv`
- `data/full_case_manifest_blinded.csv`
- `data/diagnosis_choices.json`
- 10 transcript files in `data/selected_full_cases/`

Do not place `snippet_items_key.csv` or `full_case_manifest_key.csv` in a public repository if assessors could access repository files. The app does not need either key. Keep the hidden keys outside the deployed app repository if strict blinding is required.

For Google Sheets, copy `.streamlit/secrets.toml.example` into Streamlit Cloud secrets and replace the placeholders with the service-account JSON fields. Share the target Google Sheet with the service-account email as an Editor.
