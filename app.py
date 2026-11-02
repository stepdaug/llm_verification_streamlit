# activate datascience && cd /d C:\Users\Stephen\OneDrive - Imperial College London\Research\11 headache red teaming\additional_reviewer_analyses\streamlit_app && python -m streamlit run "app.py"
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

APP_VERSION = "2026-11-02-v9"
SHEET_ID_DEFAULT = "1dfmVVaa0tD5p-JBbtNIgDT-cOzh5lFopeP7Jz4-S-Ks"
WORKSHEET_NAME_DEFAULT = "blinded_validation_responses"
DATA_DIR = Path(__file__).resolve().parent / "data"
SELECTED_CASES_DIR = DATA_DIR / "selected_full_cases"

RESPONSE_HEADERS = [
    "timestamp_utc",
    "app_version",
    "assessor_code",
    "phase",
    "item_id",
    "validation_case_id",
    "realistic",
    "most_likely_diagnosis",
    "ddx_json",
    "ct_decision",
    "mri_decision",
    "lp_decision",
    "indomethacin_decision",
    "codeine_decision",
    "triage",
    "coherent",
    "ground_truth_source",
    "ground_truth_diagnosis",
    "coherence_comment",
]

INVESTIGATION_OPTIONS = ["Perform", "Not necessary", "Insufficient information to decide"]
TREATMENT_OPTIONS = ["Should give", "Not necessary", "Insufficient information to decide"]
TRIAGE_OPTIONS = [
    "Immediate emergency assessment",
    "Urgent review within 48 hours",
    "Routine follow-up",
    "Self-management without scheduled clinician follow-up",
]


def _safe_streamlit_secrets() -> dict[str, Any]:
    """Return Streamlit secrets as a plain dict, or {} when no secrets file exists."""
    try:
        return st.secrets.to_dict()
    except Exception:
        # Streamlit raises StreamlitSecretNotFoundError when no secrets.toml exists.
        # Local testing should continue using the CSV fallback rather than crashing.
        return {}


@st.cache_resource
def get_google_worksheet():
    """Return the dedicated validation worksheet, or None for local development."""
    secrets = _safe_streamlit_secrets()
    service_info_raw = secrets.get("gcp_service_account")
    if not service_info_raw:
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        # Preserve compatibility with the secrets layout used by the previous validator app,
        # where g_sheet_url was stored inside [gcp_service_account].  It is not part of the
        # service-account credential itself, so remove it before constructing Credentials.
        info = dict(service_info_raw)
        legacy_sheet_url = info.pop("g_sheet_url", None)
        info.pop("worksheet_name", None)

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)

        sheet_id = secrets.get("sheet_id")
        sheet_url = secrets.get("g_sheet_url") or legacy_sheet_url
        worksheet_name = secrets.get("worksheet_name", WORKSHEET_NAME_DEFAULT)

        if sheet_id:
            workbook = client.open_by_key(str(sheet_id))
        elif sheet_url:
            workbook = client.open_by_url(str(sheet_url))
        else:
            workbook = client.open_by_key(SHEET_ID_DEFAULT)

        try:
            ws = workbook.worksheet(str(worksheet_name))
        except gspread.WorksheetNotFound:
            ws = workbook.add_worksheet(
                title=str(worksheet_name),
                rows=3000,
                cols=len(RESPONSE_HEADERS) + 2,
            )

        values = ws.get_all_values()
        if not values:
            # Brand-new worksheet: write the canonical schema into row 1.
            ws.update(range_name="A1", values=[RESPONSE_HEADERS], value_input_option="RAW")
        else:
            existing_header = [str(v).strip() for v in values[0]]
            expected_header = [str(v).strip() for v in RESPONSE_HEADERS]
            has_response_rows = any(
                any(str(cell).strip() for cell in row)
                for row in values[1:]
            )

            if existing_header != expected_header:
                if not has_response_rows:
                    # Safe self-repair for a newly created/empty tab whose first row is wrong,
                    # truncated or left over from an earlier app version. No response data exist.
                    ws.clear()
                    ws.update(range_name="A1", values=[RESPONSE_HEADERS], value_input_option="RAW")
                elif existing_header == expected_header[: len(existing_header)]:
                    # Backward-compatible schema extension. New columns are appended only; existing
                    # response rows are preserved and receive blank values in the new columns.
                    ws.update(range_name="A1", values=[RESPONSE_HEADERS], value_input_option="RAW")
                else:
                    # Never destroy previously collected assessor data automatically.
                    raise RuntimeError(
                        f"The '{worksheet_name}' worksheet contains response rows but its header does not "
                        "match the current blinded-validation schema. Preserve that tab and choose a new "
                        "worksheet_name in secrets.toml, or migrate the existing data manually."
                    )
        return ws
    except Exception as exc:
        st.error(f"Google Sheets connection failed: {exc}")
        return None


def local_response_path() -> Path:
    return DATA_DIR / "validation_responses_local.csv"


def load_existing_responses() -> pd.DataFrame:
    ws = get_google_worksheet()
    if ws is not None:
        records = ws.get_all_records()
        return pd.DataFrame(records, columns=RESPONSE_HEADERS)
    path = local_response_path()
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=RESPONSE_HEADERS)


def save_response(row: dict[str, Any]) -> None:
    complete = {h: row.get(h, "") for h in RESPONSE_HEADERS}
    ws = get_google_worksheet()
    if ws is not None:
        ws.append_row([complete[h] for h in RESPONSE_HEADERS], value_input_option="RAW")
        return

    # Local fallback is useful for testing before deployment. It is clearly labelled in the UI.
    path = local_response_path()
    df = pd.DataFrame([complete])
    if path.exists():
        df.to_csv(path, mode="a", header=False, index=False)
    else:
        df.to_csv(path, index=False)


def stable_order(values: list[str], assessor_code: str, salt: str) -> list[str]:
    seed_hex = hashlib.sha256(f"{assessor_code}|{salt}".encode("utf-8")).hexdigest()[:16]
    seed = int(seed_hex, 16)
    ordered = list(values)
    # A tiny deterministic Fisher-Yates shuffle avoids importing random global state.
    for i in range(len(ordered) - 1, 0, -1):
        seed = (6364136223846793005 * seed + 1442695040888963407) & ((1 << 64) - 1)
        j = seed % (i + 1)
        ordered[i], ordered[j] = ordered[j], ordered[i]
    return ordered


def completed_ids(responses: pd.DataFrame, assessor_code: str, phase: str, id_col: str) -> set[str]:
    if responses.empty or id_col not in responses.columns:
        return set()
    mask = (
        responses.get("assessor_code", pd.Series(dtype=str)).astype(str).eq(assessor_code)
        & responses.get("phase", pd.Series(dtype=str)).astype(str).eq(phase)
    )
    return set(responses.loc[mask, id_col].dropna().astype(str))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_snippets() -> pd.DataFrame:
    path = DATA_DIR / "snippet_items_blinded.csv"
    if not path.exists():
        return pd.DataFrame(columns=["item_id", "snippet"])
    return pd.read_csv(path)


@st.cache_data
def load_snippet_key() -> pd.DataFrame:
    """Load hidden snippet truth labels for storage only; these are never rendered in the UI."""
    path = DATA_DIR / "snippet_items_key.csv"
    if not path.exists():
        return pd.DataFrame(columns=["item_id", "source", "diagnosis_gt"])
    return pd.read_csv(path)


@st.cache_data
def load_case_key() -> pd.DataFrame:
    """Load hidden clinical-case truth labels for storage only; these are never rendered in the UI."""
    path = DATA_DIR / "full_case_manifest_key.csv"
    if not path.exists():
        return pd.DataFrame(columns=["validation_case_id", "gt_diagnosis"])
    return pd.read_csv(path)


def _clean_truth_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def snippet_truth(item_id: str) -> tuple[str, str]:
    """Return source and diagnosis for the result sheet without exposing either to assessors."""
    key = load_snippet_key()
    if key.empty or "item_id" not in key.columns:
        return "", ""
    match = key.loc[key["item_id"].astype(str) == str(item_id)]
    if match.empty:
        return "", ""
    row = match.iloc[0]
    raw_source = _clean_truth_value(row.get("source", ""))
    source = {
        "synthetic_headache": "headache",
        "headache": "headache",
        "primock": "primock",
        "foil": "foil",
    }.get(raw_source.lower(), raw_source)
    diagnosis = _clean_truth_value(row.get("diagnosis_gt", "")) if source == "headache" else ""
    return source, diagnosis


def case_truth(validation_case_id: str) -> tuple[str, str]:
    """Return hidden ground truth for the result sheet only."""
    key = load_case_key()
    if key.empty or "validation_case_id" not in key.columns:
        return "headache", ""
    match = key.loc[key["validation_case_id"].astype(str) == str(validation_case_id)]
    if match.empty:
        return "headache", ""
    return "headache", _clean_truth_value(match.iloc[0].get("gt_diagnosis", ""))


def stable_snippet_order(snippets: pd.DataFrame, assessor_code: str) -> list[str]:
    """Deterministic random order with positions 1-2 non-foil and position 3 a foil."""
    ids = snippets["item_id"].astype(str).tolist()
    base = stable_order(ids, assessor_code, "snippets")
    key = load_snippet_key()
    if key.empty or "source" not in key.columns or "item_id" not in key.columns:
        return base

    foil_ids = set(
        key.loc[key["source"].astype(str).str.lower().eq("foil"), "item_id"].astype(str)
    )
    nonfoils = [item for item in base if item not in foil_ids]
    foils = [item for item in base if item in foil_ids]
    if len(nonfoils) < 2 or not foils:
        return base

    # Give assessors two plausible examples before the first deliberately impossible foil.
    first_two = nonfoils[:2]
    first_foil = foils[0]
    fixed = set(first_two + [first_foil])
    remaining = [item for item in base if item not in fixed]
    return first_two + [first_foil] + remaining


def load_cases() -> pd.DataFrame:
    path = DATA_DIR / "full_case_manifest_blinded.csv"
    if not path.exists():
        return pd.DataFrame(columns=["validation_case_id", "case_summary_file"])
    return pd.read_csv(path)


def load_diagnoses() -> list[str]:
    """Load the DDx checkbox options only from data/diagnosis_choices.json."""
    path = DATA_DIR / "diagnosis_choices.json"
    if not path.exists():
        st.error("Missing data/diagnosis_choices.json. Differential-diagnosis options cannot be loaded.")
        st.stop()

    try:
        choices = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        st.error(f"Could not read data/diagnosis_choices.json: {exc}")
        st.stop()

    if not isinstance(choices, list):
        st.error("data/diagnosis_choices.json must contain a JSON list of diagnosis names.")
        st.stop()

    # Preserve the order in the data file while removing blanks and accidental duplicates.
    cleaned = []
    seen = set()
    for value in choices:
        diagnosis = str(value).strip()
        if diagnosis and diagnosis not in seen:
            cleaned.append(diagnosis)
            seen.add(diagnosis)

    if not cleaned:
        st.error("data/diagnosis_choices.json contains no diagnosis choices.")
        st.stop()
    return cleaned


def validate_truth_keys() -> bool:
    """Require hidden truth keys so every newly saved response carries its analysis labels."""
    snippet_key = load_snippet_key()
    case_key = load_case_key()
    problems = []
    if snippet_key.empty or not {"item_id", "source"}.issubset(snippet_key.columns):
        problems.append("data/snippet_items_key.csv")
    if case_key.empty or not {"validation_case_id", "gt_diagnosis"}.issubset(case_key.columns):
        problems.append("data/full_case_manifest_key.csv")
    if problems:
        st.error(
            "Hidden analysis key file(s) are missing or incomplete: " + ", ".join(problems) +
            ". Rerun additional_reviewer_analyses.py before collecting responses."
        )
        return False
    return True


def show_completion_dashboard(assessor_code: str, responses: pd.DataFrame) -> None:
    """Show compact persistent completion status derived from saved responses."""
    snippets = load_snippets()
    cases = load_cases()

    snippet_total = len(snippets)
    case_total = len(cases)
    snippet_done = len(completed_ids(responses, assessor_code, "snippet", "item_id"))
    case_done = len(completed_ids(responses, assessor_code, "full_case", "validation_case_id"))

    snippet_done = min(snippet_done, snippet_total) if snippet_total else 0
    case_done = min(case_done, case_total) if case_total else 0
    total = snippet_total + case_total
    done = snippet_done + case_done

    st.caption(
        f"Progress: statements {snippet_done}/{snippet_total or 30} | "
        f"clinical cases {case_done}/{case_total or 10} | overall {done}/{total or 40}"
    )
    st.progress(done / total if total else 0.0)

    if snippet_total and snippet_done < snippet_total:
        st.caption(f"Resume point: statement {snippet_done + 1} of {snippet_total}")
    elif case_total and case_done < case_total:
        st.caption(f"Resume point: clinical case {case_done + 1} of {case_total}")
    elif total and done >= total:
        st.success("All validation items are complete.")


def compact_summary_html(summary_text: str) -> str:
    """Render the exact stored summary text compactly without Markdown list spacing."""
    lines = [line.strip() for line in summary_text.splitlines() if line.strip()]
    rendered = []
    for line in lines:
        # Preserve the stored wording while interpreting only simple **heading** markers.
        if line.startswith("**") and line.endswith("**") and len(line) > 4:
            label = html.escape(line[2:-2].strip())
            rendered.append(f'<div class="case-summary-heading">{label}</div>')
        else:
            rendered.append(f'<div class="case-summary-line">{html.escape(line)}</div>')
    return '<div class="case-summary-box">' + ''.join(rendered) + '</div>'

def render_snippet_phase(assessor_code: str, responses: pd.DataFrame) -> bool:
    snippets = load_snippets()
    if snippets.empty:
        st.warning("The blinded snippet set has not yet been prepared.")
        return True

    done = completed_ids(responses, assessor_code, "snippet", "item_id")
    order = stable_snippet_order(snippets, assessor_code)
    remaining = [x for x in order if x not in done]
    total = len(order)
    completed = total - len(remaining)

    # Once Part 1 is complete, do not render its heading or completion panel again.
    if not remaining:
        return True

    st.markdown("#### Part 1: short patient statements")
    st.progress(completed / total if total else 1.0, text=f"{completed} of {total} completed")

    item_id = remaining[0]
    row = snippets.loc[snippets["item_id"].astype(str) == item_id].iloc[0]
    st.caption(f"Statement {completed + 1} of {total}")
    st.markdown(f"<div class='snippet-box'>{html.escape(str(row['snippet']))}</div>", unsafe_allow_html=True)

    with st.form(key=f"snippet_{item_id}", clear_on_submit=True):
        realistic = st.radio(
            "Is this an internally consistent and realistic potential description that could describe a patient's symptoms?",
            ["Internally consistent", "Internally inconsistent and not at all realistic"],
            index=None,
            horizontal=True,
        )
        submitted = st.form_submit_button("Save and continue", type="primary")
        if submitted:
            if realistic is None:
                st.warning("Please select one option.")
            else:
                truth_source, truth_diagnosis = snippet_truth(item_id)
                save_response(
                    {
                        "timestamp_utc": utc_now(),
                        "app_version": APP_VERSION,
                        "assessor_code": assessor_code,
                        "phase": "snippet",
                        "item_id": item_id,
                        "realistic": realistic,
                        "ground_truth_source": truth_source,
                        "ground_truth_diagnosis": truth_diagnosis,
                    }
                )
                st.rerun()
    return False


def render_case_phase(assessor_code: str, responses: pd.DataFrame) -> bool:
    cases = load_cases()
    if cases.empty:
        st.info(
            "Part 2 has not yet been prepared. The study organiser needs to select exactly 10 "
            "headache cases and rerun additional_reviewer_analyses.py."
        )
        return False

    diagnoses = load_diagnoses()
    done = completed_ids(responses, assessor_code, "full_case", "validation_case_id")
    order = sorted(
        cases["validation_case_id"].astype(str).tolist(),
        key=lambda x: int(x[1:]) if x.startswith("C") and x[1:].isdigit() else x,
    )
    remaining = [x for x in order if x not in done]
    total = len(order)
    completed = total - len(remaining)

    if not remaining:
        st.success("All validation items are complete. Thank you.")
        return True

    validation_case_id = remaining[0]
    row = cases.loc[cases["validation_case_id"].astype(str) == validation_case_id].iloc[0]
    summary_path = SELECTED_CASES_DIR / str(row["case_summary_file"])
    if not summary_path.exists():
        st.error(
            f"Case summary missing: {row['case_summary_file']}. "
            "Rerun additional_reviewer_analyses.py to regenerate C01-C10 before collecting responses."
        )
        return False
    try:
        summary_text = summary_path.read_text(encoding="utf-8")
    except Exception as exc:
        st.error(f"Could not read case summary: {exc}")
        return False

    st.markdown(
        f"<div class='case-topline'><strong>Part 2: blinded clinical case</strong> &nbsp; "
        f"{validation_case_id} &nbsp; | &nbsp; Case {completed + 1} of {total}</div>",
        unsafe_allow_html=True,
    )

    left, right = st.columns([1.05, 1.0], gap="small")
    with left:
        st.markdown(compact_summary_html(summary_text), unsafe_allow_html=True)

    with right:
        with st.form(key=f"case_{validation_case_id}"):
            st.markdown("**Differential diagnosis**")
            st.caption("Tick all diagnoses that could reasonably be included in the differential diagnosis.")
            ddx = []
            ddx_cols = st.columns(3, gap="small")
            for idx, diagnosis in enumerate(diagnoses):
                with ddx_cols[idx % 3]:
                    if st.checkbox(diagnosis, key=f"ddx_{idx}_{validation_case_id}"):
                        ddx.append(diagnosis)

            st.markdown("**Investigations**")
            ct = st.radio(
                "CT head",
                INVESTIGATION_OPTIONS,
                index=None,
                horizontal=True,
                key=f"ct_{validation_case_id}",
            )
            mri = st.radio(
                "MRI head",
                INVESTIGATION_OPTIONS,
                index=None,
                horizontal=True,
                key=f"mri_{validation_case_id}",
            )
            lp = st.radio(
                "Lumbar puncture",
                INVESTIGATION_OPTIONS,
                index=None,
                horizontal=True,
                key=f"lp_{validation_case_id}",
            )

            st.markdown("**Treatment**")
            indo = st.radio(
                "Indomethacin",
                TREATMENT_OPTIONS,
                index=None,
                horizontal=True,
                key=f"indo_{validation_case_id}",
            )
            codeine = st.radio(
                "Codeine",
                TREATMENT_OPTIONS,
                index=None,
                horizontal=True,
                key=f"codeine_{validation_case_id}",
            )

            st.markdown("**Triage**")
            st.caption("Select all clinically acceptable options.")
            triage_selected = []
            t1, t2 = st.columns(2, gap="small")
            for idx, option in enumerate(TRIAGE_OPTIONS):
                target = t1 if idx % 2 == 0 else t2
                with target:
                    if st.checkbox(option, key=f"triage_{idx}_{validation_case_id}"):
                        triage_selected.append(option)

            st.markdown("**Clinical plausibility**")
            implausible = st.radio(
                "Are there any definite clinically implausible or internally inconsistent features?",
                ["No definite implausibility", "Yes, there is a definite implausibility"],
                index=None,
                horizontal=True,
                key=f"implausible_{validation_case_id}",
            )
            coherence_comment = ""
            if implausible == "Yes, there is a definite implausibility":
                coherence_comment = st.text_area(
                    "Please specify the definite implausible or internally inconsistent feature(s).",
                    key=f"coherence_comment_{validation_case_id}",
                    height=80,
                )

            submitted = st.form_submit_button("Save case and continue", type="primary", use_container_width=True)
            if submitted:
                required = [ct, mri, lp, indo, codeine, implausible]
                if not ddx:
                    st.warning("Please select at least one diagnosis that could reasonably be included in the differential diagnosis.")
                elif not triage_selected:
                    st.warning("Please select at least one acceptable triage option.")
                elif not all(required):
                    st.warning("Please complete all investigation, treatment and plausibility fields.")
                elif implausible == "Yes, there is a definite implausibility" and not coherence_comment.strip():
                    st.warning("Please specify the definite implausible or internally inconsistent feature(s).")
                else:
                    truth_source, truth_diagnosis = case_truth(validation_case_id)
                    save_response(
                        {
                            "timestamp_utc": utc_now(),
                            "app_version": APP_VERSION,
                            "assessor_code": assessor_code,
                            "phase": "full_case",
                            "validation_case_id": validation_case_id,
                            "most_likely_diagnosis": "",
                            "ddx_json": json.dumps(list(dict.fromkeys(ddx))),
                            "ct_decision": ct,
                            "mri_decision": mri,
                            "lp_decision": lp,
                            "indomethacin_decision": indo,
                            "codeine_decision": codeine,
                            "triage": json.dumps(triage_selected),
                            "coherent": "Yes" if implausible == "No definite implausibility" else "No",
                            "coherence_comment": coherence_comment.strip(),
                            "ground_truth_source": truth_source,
                            "ground_truth_diagnosis": truth_diagnosis,
                        }
                    )
                    st.rerun()
    return False

def main() -> None:
    st.set_page_config(page_title="Blinded clinical validation", page_icon="🩺", layout="wide")
    st.markdown(
        """
<style>
.block-container {
    padding-top: 0.7rem;
    padding-bottom: 0.6rem;
    max-width: 1550px;
}
[data-testid="stVerticalBlock"] { gap: 0.28rem; }
[data-testid="stHorizontalBlock"] { gap: 0.55rem; }
[data-testid="stMarkdownContainer"] p { margin: 0.10rem 0 0.16rem 0; }
[data-testid="stCaptionContainer"] { margin-top: -0.10rem; margin-bottom: 0.05rem; }
div[data-testid="stForm"] { padding: 0.45rem 0.65rem 0.55rem 0.65rem; }
div[data-testid="stSelectbox"], div[data-testid="stMultiSelect"], div[data-testid="stRadio"], div[data-testid="stCheckbox"] { margin-bottom: -0.12rem; }
label[data-testid="stWidgetLabel"] p { font-size: 0.90rem; margin-bottom: 0.05rem; }
.case-topline { font-size: 0.95rem; margin: 0 0 0.30rem 0; }
.case-summary-box {
    border: 1px solid rgba(128,128,128,0.25);
    border-radius: 0.35rem;
    padding: 0.48rem 0.62rem;
    font-size: 0.88rem;
    line-height: 1.16;
    margin: 0;
}
.case-summary-heading { font-weight: 700; margin-top: 0.26rem; margin-bottom: 0.04rem; }
.case-summary-heading:first-child { margin-top: 0; }
.case-summary-line { margin: 0.02rem 0; }
.snippet-box {
    border-left: 3px solid rgba(128,128,128,0.45);
    padding: 0.40rem 0.65rem;
    margin: 0.15rem 0 0.35rem 0;
    font-size: 1rem;
}
h1, h2, h3, h4, h5 { margin-top: 0.15rem !important; margin-bottom: 0.20rem !important; }
hr { margin: 0.35rem 0 !important; }
</style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("### Blinded clinical validation")
    st.caption(
        "Complete the short patient statements first, then the blinded clinical cases. "
        "Sources, diagnoses and reference labels are hidden."
    )

    if get_google_worksheet() is None:
        st.warning(
            "Google Sheets is not connected. Responses will be saved locally for testing only. "
            "Add .streamlit/secrets.toml before sending this app to assessors."
        )

    if not validate_truth_keys():
        st.stop()

    assessor_code = st.selectbox(
        "Assessor initials",
        options=["", "NG", "AV", "testing"],
        index=0,
        help="Select your initials before starting. Responses are stored separately for each assessor.",
    )
    if assessor_code not in {"NG", "AV", "testing"}:
        st.stop()

    responses = load_existing_responses()
    show_completion_dashboard(assessor_code, responses)
    snippets_complete = render_snippet_phase(assessor_code, responses)
    if snippets_complete:
        responses = load_existing_responses()
        render_case_phase(assessor_code, responses)


if __name__ == "__main__":
    main()
