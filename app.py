"""MILO R5 Source Capture - Streamlit entrypoint.

A temporary evidence-capture tool. It performs a fixed, bounded set of public
read-only HTTP GET requests from the Streamlit server, preserves the returned
bodies byte-for-byte, validates them, and offers a verified ZIP for download.

There is deliberately no URL input, no database, no model provider, no AI API,
no user account and no secret of any kind.
"""

import streamlit as st

import capture

STATE_KEY = "milo_r5_capture_result"

STATUS_PRESENTATION = {
    capture.STATUS_READY: (
        "success",
        "Ready for R5 bundle review",
        "All mandatory sources passed, at least one Government query returned "
        "records, and the Toyota HTML contains technical markers.",
    ),
    capture.STATUS_NO_RECORDS: (
        "warning",
        "No matching Government records",
        "All Government calls were valid, but every bounded query returned zero "
        "records.",
    ),
    capture.STATUS_INCOMPLETE_WEB: (
        "warning",
        "Incomplete official web source",
        "Government evidence passed, but the saved Toyota HTML lacked usable "
        "technical content.",
    ),
    capture.STATUS_FAILED: (
        "error",
        "Capture failed",
        "A network, HTTP, redirect, size, JSON, blocking-page or integrity "
        "failure occurred during the capture.",
    ),
}


def _planned_sources():
    return [
        (source.source_id, source.source_type, source.url)
        for source in capture.build_base_plan()
    ]


def _results_rows(entries):
    rows = []
    for entry in entries:
        rows.append(
            {
                "source_id": entry["source_id"],
                "type": entry["source_type"],
                "status": entry["http_status"],
                "validation": entry["validation_result"],
                "detail": entry["validation_detail"],
                "records": entry.get("record_count"),
                "bytes": entry["byte_count"],
                "attempts": entry["attempts"],
                "redirects": len(entry["redirect_chain"]),
                "sha256": entry["sha256"][:16] + "...",
                "error": entry["error"] or "",
            }
        )
    return rows


def _render_status(result):
    kind, headline, explanation = STATUS_PRESENTATION.get(
        result["overall_status"],
        ("error", "Unknown status", "The capture returned an unrecognised status."),
    )
    banner = {"success": st.success, "warning": st.warning, "error": st.error}[kind]
    banner("**{0}**\n\n`{1}`\n\n{2}".format(headline, result["overall_status"], explanation))

    if result["overall_status"] != capture.STATUS_READY:
        st.warning(
            "**This archive is INCOMPLETE and is not R5-ready.** It is a "
            "diagnostic bundle. Do not treat it as a verified R5 evidence set, "
            "and do not relabel it as one. The filename is prefixed "
            "`milo-r5-source-capture-INCOMPLETE-` for this reason."
        )

    toyota = [
        e for e in result["entries"] if e["source_type"] == capture.SOURCE_TYPE_WEB
    ]
    for entry in toyota:
        if entry["validation_result"] == capture.TOYOTA_RESULT_CANDIDATE:
            st.info(
                "Toyota page: `{0}`. Technical markers were found in the saved "
                "HTML, but **no vehicle fact has been established**. Nothing was "
                "inferred from the URL or filename. A human must read the "
                "preserved HTML to confirm any specification.".format(
                    entry["validation_result"]
                )
            )


def _render_integrity(result):
    if result["integrity_verified"]:
        st.caption(
            "Integrity verified: body byte counts and SHA-256 digests were "
            "recalculated and checked against manifest.json, manifest.json was "
            "hashed, and SHA256SUMS.txt was generated and verified before the "
            "archive was built."
        )
    else:
        st.error(
            "Archive integrity verification FAILED:\n\n"
            + "\n".join("- {0}".format(p) for p in result["integrity_problems"])
        )


def main():
    st.set_page_config(page_title="MILO R5 Source Capture", page_icon="\U0001f4e6")

    st.title("MILO R5 Source Capture")
    st.write(
        "Captures a fixed, bounded set of **public** Government (data.gov.il "
        "CKAN) and official Toyota sources from the Streamlit server, preserves "
        "the response bodies byte-for-byte, validates them, and produces a "
        "verified evidence ZIP for MILO R5."
    )

    st.info(
        "**No API key is required and none is used.** Every source is a public, "
        "read-only endpoint. This app sends `GET` requests only. It uses no "
        "Authorization header, no CKAN API token, no Streamlit secret, no "
        "cookie and no credential of any kind. There is no URL input: the "
        "request set is fixed in code. Full datasets are never downloaded - "
        "schema probes use `limit=0` and record queries use `limit={0}`.".format(
            capture.QUERY_LIMIT
        )
    )

    with st.expander("Exactly which requests will be made"):
        st.caption(
            "These six requests always run, in this order. Two extra bounded "
            "queries per resource run only if a valid RAV4 query returns zero "
            "records."
        )
        for source_id, source_type, url in _planned_sources():
            st.markdown("- `{0}` ({1})  \n  {2}".format(source_id, source_type, url))

    run_clicked = st.button("Run bounded R5 capture", type="primary")
    reset_clicked = st.button("Reset capture")

    if reset_clicked:
        st.session_state.pop(STATE_KEY, None)
        st.rerun()

    if run_clicked:
        progress_bar = st.progress(0.0, text="Starting bounded capture...")

        def on_progress(source_id, completed, planned):
            fraction = min(completed / float(max(planned, 1)), 1.0)
            progress_bar.progress(
                fraction, text="Requesting {0} ({1}/{2})".format(source_id, completed + 1, planned)
            )

        # Deliberately not cached: every run performs its own live requests.
        result = capture.run_and_package(progress=on_progress)
        progress_bar.progress(1.0, text="Capture complete")
        st.session_state[STATE_KEY] = result

    result = st.session_state.get(STATE_KEY)
    if not result:
        st.caption("No capture has been run in this session yet.")
        return

    _render_status(result)

    st.subheader("Results")
    st.dataframe(_results_rows(result["entries"]))

    _render_integrity(result)

    st.subheader("Download")
    st.caption(
        "Archive `{0}` - {1} bytes, sha256 `{2}`.".format(
            result["archive_filename"], result["archive_byte_count"], result["archive_sha256"]
        )
    )
    st.download_button(
        label="Download {0}".format(result["archive_filename"]),
        data=result["archive_bytes"],
        file_name=result["archive_filename"],
        mime="application/zip",
    )


if __name__ == "__main__":
    main()
