"""
app_email_ext.py
----------------
Email integration extension for app.py. Self-contained module that:
  - Builds the raw KPI / flagged-UC context needed for the Cortex summary
  - Handles the POST /api/prepare_email endpoint
  - Provides the HTML / CSS / JS snippets to inject into the dashboard

Why a separate module:
  - Keeps app.py changes to ~4 small additive edits (easy to diff vs. original)
  - All Cortex / email imports are isolated here; if any fail, the dashboard
    still works for everything else
  - All threshold logic and prompt context live in one place for future tweaking

Module-level state is intentionally minimal — the dashboard owns
pipeline_status; this module just reads/writes the 'email_context' key.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path
from typing import Optional

# Thresholds match the existing report-side flagging logic.
FLAG_ACCEPTANCE_BELOW = 0.50
FLAG_DISMISSAL_ABOVE = 0.20
FLAG_NO_ACTION_ABOVE = 0.20

# Volume gate — flagged items below this become "caveat" items in the email,
# never the substantive review list. Matches the Italy 2025 review rule.
MIN_VOLUME_FOR_CONCLUSION = 50

# Strong-performer tiers — a use case is celebrated only with sufficient volume
# AND clean friction (dismissal/no-action within thresholds) AND high acceptance.
# The friction caps guarantee a strong UC can never also be a flagged one, so the
# two buckets stay mutually exclusive (no conflicting placement).
STRONG_ACCEPTANCE_GOOD = 0.70       # 70-80% acceptance -> "good"
STRONG_ACCEPTANCE_VERY_GOOD = 0.80  # 80%+ acceptance   -> "very good" (highlight)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email modules (deferred import — dashboard keeps working if any are missing)
# ---------------------------------------------------------------------------

try:
    from email_router import lookup_owner, RoutingError
    from cortex_summary import generate_email_summary, generate_exec_summary, CortexError
    from email_sender_eml import EmailPayload, send_email
    EMAIL_AVAILABLE = True
    EMAIL_IMPORT_ERROR: Optional[str] = None
except Exception as e:  # ImportError or any init failure
    EMAIL_AVAILABLE = False
    EMAIL_IMPORT_ERROR = str(e)
    log.warning("Email extension disabled: %s", e)


# ---------------------------------------------------------------------------
# 1) Build the email context from raw KPI results
# ---------------------------------------------------------------------------

def compute_email_context(
    country_code: Optional[str],
    country_label: str,
    country_name: Optional[str],
    period: str,
    df_row_count: int,
    kpi_results: dict,
    pptx_path,
) -> dict:
    """
    Snapshot the data the email needs from a finished pipeline run.

    Pulled fresh from kpi_results (not from the formatted dashboard strings)
    because the Cortex prompt needs raw floats and counts, not "65.8%".
    """
    import pandas as pd  # local — keep app.py imports minimal

    overall = kpi_results.get("overall", pd.DataFrame())
    ov = overall.iloc[0] if not overall.empty else {}

    by_uc = kpi_results.get("by_usecase", pd.DataFrame())
    by_bu = kpi_results.get("by_brand_usecase", pd.DataFrame())

    # Flagged UCs — apply the same thresholds the report side uses.
    # First match wins (acceptance, then dismissal, then no-action).
    flagged = []
    if not by_bu.empty:
        for _, r in by_bu.iterrows():
            acc = float(r.get("acceptance_rate_total", 0) or 0)
            dis = float(r.get("dismissal_rate_total", 0) or 0)
            nac = float(r.get("no_action_rate", 0) or 0)
            n = int(r.get("total_suggestions", 0) or 0)
            metric = value = None
            if acc < FLAG_ACCEPTANCE_BELOW:
                metric, value = "low acceptance", acc
            elif dis > FLAG_DISMISSAL_ABOVE:
                metric, value = "dismissal", dis
            elif nac > FLAG_NO_ACTION_ABOVE:
                metric, value = "no action", nac
            if metric:
                flagged.append({
                    "brand": str(r["medicine"]),
                    "usecase": str(r["sugg_name"]),
                    "metric": metric,
                    "value": value,
                    "suggestions": n,
                })

    # Strong performers — sufficient volume, clean friction, high acceptance.
    # The friction caps (<= the flag thresholds) mean a strong UC can never also
    # satisfy a flag condition, so it never collides with flagged_ucs. Best-first.
    strong = []
    if not by_bu.empty:
        for _, r in by_bu.iterrows():
            acc = float(r.get("acceptance_rate_total", 0) or 0)
            dis = float(r.get("dismissal_rate_total", 0) or 0)
            nac = float(r.get("no_action_rate", 0) or 0)
            n = int(r.get("total_suggestions", 0) or 0)
            if (n >= MIN_VOLUME_FOR_CONCLUSION
                    and acc >= STRONG_ACCEPTANCE_GOOD
                    and dis <= FLAG_DISMISSAL_ABOVE
                    and nac <= FLAG_NO_ACTION_ABOVE):
                tier = "very good" if acc >= STRONG_ACCEPTANCE_VERY_GOOD else "good"
                strong.append({
                    "brand": str(r["medicine"]),
                    "usecase": str(r["sugg_name"]),
                    "acceptance": acc,
                    "suggestions": n,
                    "tier": tier,
                })
        strong.sort(key=lambda u: u["acceptance"], reverse=True)

    return {
        "country_code": country_code,
        "country_label": country_label,
        "country_name": country_name or country_label,
        "period": period,
        "kpis": {
            "total_suggestions": int(ov.get("total_suggestions", df_row_count) or 0),
            "acceptance_rate": float(ov.get("acceptance_rate_total", 0) or 0),
            "dismissal_rate": float(ov.get("dismissal_rate_total", 0) or 0),
            "no_action_rate": float(ov.get("no_action_rate", 0) or 0),
            "total_ucs": int(len(by_uc)) if not by_uc.empty else 0,
        },
        "flagged_ucs": flagged,
        "strong_ucs": strong,
        "pptx_path": str(pptx_path) if pptx_path else "",
    }


# ---------------------------------------------------------------------------
# 2) POST /api/prepare_email handler
# ---------------------------------------------------------------------------

def handle_prepare_email(handler, pipeline_status: dict) -> None:
    """
    Called from PipelineHandler.do_POST when path == /api/prepare_email.

    Reads pipeline_status["email_context"], generates the Cortex summary,
    writes the .eml, returns JSON with preview + download URL.

    Writes a response on `handler` (the HTTP handler instance). Does not raise.
    """
    def reply(code: int, body: dict) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(body).encode())

    if not EMAIL_AVAILABLE:
        log.warning("email modules not loaded: %s", EMAIL_IMPORT_ERROR)
        return reply(500, {"ok": False,
                           "error": "AI summary modules are not available. Contact the admin."})

    ctx = pipeline_status.get("email_context")
    if not ctx:
        return reply(400, {"ok": False,
                           "error": "No pipeline run available. Generate a report first."})

    if not ctx.get("country_code"):
        return reply(400, {"ok": False,
                           "error": ("Email send requires a specific country. "
                                     "Re-run the analysis for a single country.")})

    # 1) Look up owner
    try:
        owner = lookup_owner(ctx["country_code"])
    except RoutingError as e:
        # Don't leak the absolute CSV path or raw error text into the UI;
        # log it server-side and give the user something actionable.
        log.warning("routing lookup failed for %s: %s", ctx["country_code"], e)
        return reply(400, {
            "ok": False,
            "error": (f"No email address on file for {ctx['country_code']}. "
                      f"Add a row for it in config/country_owners.csv, "
                      f"or choose a market that has been configured."),
        })

    if not owner.is_resolved:
        return reply(400, {"ok": False,
                           "error": (f"No email on file for {ctx['country_code']}. "
                                     f"Edit config/country_owners.csv first.")})

    # 2) Generate AI summary (this is the slow step, ~26-32s)
    try:
        body_core = generate_email_summary(
            country=ctx["country_name"],
            kpis=ctx["kpis"],
            flagged_ucs=ctx["flagged_ucs"],
            strong_ucs=ctx.get("strong_ucs", []),
            period=ctx["period"],
        )
    except CortexError as e:
        # Don't expose the gateway name in user-facing errors.
        log.warning("AI summary failed for %s: %s", ctx["country_code"], e)
        return reply(500, {"ok": False,
                           "error": "AI summary generation failed. Please retry."})

    # 3) Wrap with greeting + sign-off (email layer concern, not Cortex's)
    full_body = (
        f"Hello {owner.name},\n\n"
        f"{body_core}\n\n"
        f"Best regards,\n"
        f"IBU Omnichannel Analytics"
    )

    # 4) Build payload + write .eml
    subject = (f"IBU NBA/E UC Performance — {ctx['country_name']} "
               f"({ctx['period']})")
    attachment = Path(ctx["pptx_path"]) if ctx.get("pptx_path") else None
    payload = EmailPayload(
        to=owner.email,
        cc=tuple(owner.cc),
        subject=subject,
        body_text=full_body,
        attachment_path=attachment,
    )

    # mode="open" — try to launch the .eml in Outlook directly on the server.
    # Falls back to mode="save" semantics if os.startfile isn't available
    # (e.g. running on SageMaker / non-Windows), in which case we still hand
    # back a download URL.
    result = send_email(payload, mode="open")
    eml_path = str(result.eml_path) if result.eml_path else None

    if result.ok:
        # Outlook draft is now open on the server (Windows host).
        return reply(200, {
            "ok": True,
            "popped_up": True,
            "to": owner.email,
            "cc": list(owner.cc),
            "owner_name": owner.name,
            "owner_role": owner.role,
            "subject": subject,
            "has_attachment": bool(attachment),
            "n_flagged": len(ctx.get("flagged_ucs", [])),
        })

    # Pop-up failed (most likely non-Windows host). The .eml may still have
    # been written before the launch attempt — fall back to download.
    if not eml_path:
        log.warning("draft generation failed: %s", result.error)
        return reply(500, {"ok": False,
                           "error": "Failed to write draft. Please retry."})

    return reply(200, {
        "ok": True,
        "popped_up": False,
        "to": owner.email,
        "cc": list(owner.cc),
        "owner_name": owner.name,
        "owner_role": owner.role,
        "subject": subject,
        "body": full_body,
        "eml_path": eml_path,
        "eml_url": f"/api/download?path={urllib.parse.quote(eml_path)}",
        "has_attachment": bool(attachment),
        "n_flagged": len(ctx.get("flagged_ucs", [])),
        "popup_error": result.error,
    })


# ---------------------------------------------------------------------------
# 2b) Dashboard executive summary (non-blocking) + retry endpoint
# ---------------------------------------------------------------------------

def safe_generate_ai_summary(ctx: Optional[dict]) -> dict:
    """
    Generate the dashboard exec summary WITHOUT ever raising. Used inside the
    pipeline thread so a Cortex error or timeout never blocks results from
    rendering. Returns {"summary": str | None, "error": str | None}.
    """
    if not EMAIL_AVAILABLE:
        log.warning("AI summary modules not loaded: %s", EMAIL_IMPORT_ERROR)
        return {"summary": None, "error": "AI summary is unavailable on this host."}
    if not ctx:
        return {"summary": None, "error": "No analysis context available."}
    try:
        text = generate_exec_summary(
            country=ctx.get("country_name") or ctx.get("country_label", ""),
            kpis=ctx.get("kpis", {}),
            flagged_ucs=ctx.get("flagged_ucs", []),
            strong_ucs=ctx.get("strong_ucs", []),
            period=ctx.get("period"),
        )
        return {"summary": text, "error": None}
    except CortexError as e:
        log.warning("AI exec summary failed: %s", e)
        return {"summary": None, "error": "AI summary unavailable \u2014 click Retry to try again."}
    except Exception as e:  # defensive: the pipeline thread must never crash here
        log.warning("AI exec summary unexpected error: %s", e)
        return {"summary": None, "error": "AI summary unavailable \u2014 click Retry to try again."}


def handle_ai_summary(handler, pipeline_status: dict) -> None:
    """
    POST /api/ai_summary \u2014 (re)generate the dashboard exec summary on demand
    (the Retry button). Reads the snapshot left by the last pipeline run.
    Writes a JSON response on `handler`. Does not raise.
    """
    def reply(code: int, body: dict) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(body).encode())

    ctx = pipeline_status.get("email_context")
    if not ctx:
        return reply(400, {"ok": False,
                           "error": "No analysis available. Generate a report first."})

    result = safe_generate_ai_summary(ctx)
    if result["summary"]:
        return reply(200, {"ok": True, "summary": result["summary"]})
    return reply(200, {"ok": False, "error": result["error"] or "AI summary unavailable."})


# ---------------------------------------------------------------------------
# 3) HTML / CSS / JS snippets to inject into app.py's HTML_PAGE
# ---------------------------------------------------------------------------

# CSS — adds modal styles and the "Send to Stakeholders" button styling.
# Insert just before the closing </style> tag.
EMAIL_CSS = """
/* === Email integration === */
.btn-secondary {
    background: #1E2761; color: white; border: none; padding: 12px 30px;
    border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer;
    transition: background 0.2s; margin-left: 10px;
}
.btn-secondary:hover { background: #14193f; }
.btn-secondary:disabled { background: #ccc; cursor: not-allowed; }
.send-actions { margin-top: 8px; }
.modal-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5); z-index: 100; align-items: center;
    justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal {
    background: white; border-radius: 10px; max-width: 720px; width: 92%;
    max-height: 88vh; overflow-y: auto; padding: 25px;
}
.modal h2 { color: #D52B1E; margin-bottom: 15px; border-bottom: 2px solid #f0f0f0; padding-bottom: 8px; }
.modal .field { margin-bottom: 12px; font-size: 13px; }
.modal .field label { font-weight: 600; color: #555; display: block; margin-bottom: 4px; }
.modal .field .value { background: #f7f8fa; padding: 8px 12px; border-radius: 4px; word-break: break-word; }
.modal .body-preview {
    background: #f7f8fa; padding: 12px 16px; border-radius: 4px;
    white-space: pre-wrap; font-family: 'Segoe UI', sans-serif; font-size: 13px;
    line-height: 1.5; max-height: 320px; overflow-y: auto;
    border-left: 3px solid #D52B1E;
}
.modal-actions { margin-top: 18px; display: flex; gap: 10px; justify-content: flex-end; }
.btn-cancel {
    background: #e0e0e0; color: #333; border: none; padding: 10px 20px;
    border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer;
}
.btn-cancel:hover { background: #c8c8c8; }
.spinner {
    display: inline-block; width: 14px; height: 14px; border: 2px solid #fff;
    border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite;
    margin-right: 8px; vertical-align: middle;
}
@keyframes spin { to { transform: rotate(360deg); } }
"""

# HTML — the "Send to Stakeholders" button row.
# Insert inside the Downloads card, just before the closing </div> of that card,
# OR as a new card after Downloads. Below it's placed as a new card.
EMAIL_BUTTON_CARD = """
        <div class="card" id="sendCard">
            <h2>Send to Stakeholders</h2>
            <p style="font-size:13px;color:#666;margin-bottom:12px;">
                Drafts a stakeholder-ready summary email to the contact on file for this market. Every figure is taken directly from this pipeline's validated analysis. The AI writes only the surrounding narrative and frames observations as hypotheses to confirm, not conclusions. The draft opens in Outlook for you to review and edit before anything is sent.
            </p>
            <div class="send-actions">
                <button class="btn-secondary" id="sendBtn" onclick="prepareEmail()">Prepare Email</button>
                <span id="sendStatus" style="font-size:13px;color:#666;margin-left:12px;"></span>
            </div>
        </div>
"""

# HTML — the preview modal. Insert just before the closing </body> tag.
EMAIL_MODAL_HTML = """
<div class="modal-overlay" id="emailModal">
    <div class="modal">
        <h2 id="emailModalTitle">Email Preview</h2>
        <div class="field"><label>To</label><div class="value" id="emailTo"></div></div>
        <div class="field" id="emailCcField" style="display:none;"><label>CC</label><div class="value" id="emailCc"></div></div>
        <div class="field"><label>Owner</label><div class="value" id="emailOwner"></div></div>
        <div class="field"><label>Subject</label><div class="value" id="emailSubject"></div></div>
        <div class="field"><label>Body (AI-generated summary)</label><div class="body-preview" id="emailBody"></div></div>
        <div class="field" id="emailAttachField" style="display:none;">
            <label>Attachment</label><div class="value">Deck PPTX attached</div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeEmailModal()">Cancel</button>
            <a id="emailDownloadLink" class="btn-secondary" href="#" download
               style="text-decoration:none;display:inline-block;">Download draft (.eml)</a>
        </div>
        <p style="font-size:12px;color:#888;margin-top:12px;">
            Double-click the downloaded .eml file to open it in Outlook as a draft.
            Review, edit if needed, then click Send.
        </p>
    </div>
</div>
"""

# JS — the prepareEmail / closeEmailModal handlers.
# Insert inside the existing <script> block, anywhere after showResults().
EMAIL_JS = """
async function prepareEmail() {
    const btn = document.getElementById('sendBtn');
    const status = document.getElementById('sendStatus');
    btn.disabled = true;
    status.style.color = '#666';
    status.innerHTML = '<span class="spinner"></span>Generating AI summary (about 30s)...';

    try {
        const resp = await fetch('/api/prepare_email', { method: 'POST' });
        const data = await resp.json();
        if (!data.ok) {
            status.textContent = 'Error: ' + (data.error || 'unknown');
            status.style.color = '#D52B1E';
            btn.disabled = false;
            return;
        }

        if (data.popped_up) {
            // Outlook draft opened on the server's desktop session.
            status.style.color = '#2e7d32';
            status.innerHTML = 'Outlook draft opened (to: ' + data.to +
                '). Review the email window and click Send when ready.';
            btn.disabled = false;
            return;
        }

        // Fallback path — couldn't launch Outlook (likely SageMaker host).
        // Show the modal with download link instead.
        document.getElementById('emailTo').textContent = data.to;
        const ccField = document.getElementById('emailCcField');
        if (data.cc && data.cc.length) {
            document.getElementById('emailCc').textContent = data.cc.join('; ');
            ccField.style.display = 'block';
        } else {
            ccField.style.display = 'none';
        }
        document.getElementById('emailOwner').textContent =
            data.owner_name + (data.owner_role ? ' (' + data.owner_role + ')' : '');
        document.getElementById('emailSubject').textContent = data.subject;
        document.getElementById('emailBody').textContent = data.body;
        document.getElementById('emailAttachField').style.display =
            data.has_attachment ? 'block' : 'none';
        document.getElementById('emailDownloadLink').href = data.eml_url;
        document.getElementById('emailModal').classList.add('show');
        status.textContent = '';
        btn.disabled = false;
    } catch (e) {
        status.textContent = 'Request failed: ' + e.message;
        status.style.color = '#D52B1E';
        btn.disabled = false;
    }
}

function closeEmailModal() {
    document.getElementById('emailModal').classList.remove('show');
}
"""


# ---------------------------------------------------------------------------
# 4) Dashboard exec-summary card snippets (injected into app.py's HTML_PAGE)
# ---------------------------------------------------------------------------

# CSS — the top-of-results exec summary card. Insert before </style>.
SUMMARY_CSS = """
/* === AI exec summary card === */
.summary-card {
    background: #fff; border: 1px solid #eac3bb; border-left: 4px solid #D52B1E;
    border-radius: 10px; padding: 20px 24px; margin-bottom: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.summary-card h2 {
    color: #D52B1E; font-size: 16px; margin-bottom: 10px;
    border: none; padding: 0; display: flex; align-items: center; gap: 8px;
}
.summary-card .ai-tag {
    font-size: 10px; font-weight: 700; letter-spacing: .04em; color: #fff;
    background: #1E2761; padding: 2px 8px; border-radius: 10px;
}
.summary-kicker {
    font-size: 11px; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; color: #1E2761; opacity: .72; margin-bottom: 6px;
}
.summary-headline {
    font-size: 16px; font-weight: 700; color: #1E2761; line-height: 1.4;
    margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid #f0e3e0;
}
.summary-points { list-style: none; margin: 0; padding: 0; }
.summary-points li {
    position: relative; padding-left: 18px; margin-bottom: 9px;
    font-family: 'Segoe UI', Arial, sans-serif; font-size: 14px;
    line-height: 1.55; color: #333;
}
.summary-points li:last-child { margin-bottom: 0; }
.summary-points li::before {
    content: ''; position: absolute; left: 2px; top: 8px; width: 6px; height: 6px;
    border-radius: 50%; background: #D52B1E;
}
.summary-para {
    font-family: 'Segoe UI', Arial, sans-serif; font-size: 14px;
    line-height: 1.55; color: #333; margin-bottom: 9px;
}
.summary-body {
    white-space: pre-wrap; font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 14px; line-height: 1.55; color: #333;
}
.summary-error { color: #b0221a; font-size: 13px; margin-bottom: 8px; }
.summary-retry {
    background: #1E2761; color: #fff; border: none; padding: 7px 16px;
    border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer;
}
.summary-retry:hover { background: #14193f; }
.summary-retry:disabled { background: #ccc; cursor: not-allowed; }
"""

# HTML — the card itself. Insert as the FIRST child of #results so it sits
# above the KPI cards. Hidden until renderAiSummary() fills it.
SUMMARY_CARD = """
        <div class="summary-card" id="summaryCard" style="display:none;">
            <h2>Executive Summary <span class="ai-tag">AI</span></h2>
            <div id="summaryContent"></div>
        </div>
"""

# JS — render + retry handlers. Insert inside the <script> block.
# renderAiSummary(r) is called by showResults(); parses the plain-text summary
# into a styled headline + bullet list and builds DOM via textContent per node
# (no HTML injection from model output).
SUMMARY_JS = r"""
function _cleanSummaryLine(s) {
    return String(s)
        .replace(/\*\*(.+?)\*\*/g, '$1')
        .replace(/\\([_*`~#>])/g, '$1')
        .trim();
}

function _renderSummaryInto(content, text) {
    content.innerHTML = '';
    if (!text) { return; }
    var lines = String(text).split(/\r?\n/);
    var ul = null;
    var headlineDone = false;
    for (var i = 0; i < lines.length; i++) {
        var line = _cleanSummaryLine(lines[i]);
        if (!line) { continue; }

        if (/^[-\u2013\u2022]\s+/.test(line)) {
            if (!ul) {
                ul = document.createElement('ul');
                ul.className = 'summary-points';
                content.appendChild(ul);
            }
            var li = document.createElement('li');
            li.textContent = line.replace(/^[-\u2013\u2022]\s+/, '');
            ul.appendChild(li);
            continue;
        }

        ul = null;
        if (!headlineDone) {
            headlineDone = true;
            var parts = line.split('|').map(function (p) { return p.trim(); })
                            .filter(function (p) { return p.length; });
            var kicker = '', headline = line;
            if (parts.length >= 3) {
                kicker = parts.slice(0, parts.length - 1).join('  \u00b7  ');
                headline = parts[parts.length - 1];
            } else if (parts.length === 2) {
                kicker = parts[0];
                headline = parts[1];
            } else if (parts.length === 1) {
                headline = parts[0];
            }
            if (kicker) {
                var k = document.createElement('div');
                k.className = 'summary-kicker';
                k.textContent = kicker;
                content.appendChild(k);
            }
            var h = document.createElement('div');
            h.className = 'summary-headline';
            h.textContent = headline;
            content.appendChild(h);
        } else {
            var p = document.createElement('div');
            p.className = 'summary-para';
            p.textContent = line;
            content.appendChild(p);
        }
    }
}

function _renderSummaryError(content, msg) {
    content.innerHTML =
        '<div class="summary-error"></div>' +
        '<button class="summary-retry" id="summaryRetryBtn" onclick="retryAiSummary()">Retry</button>';
    content.querySelector('.summary-error').textContent = msg;
}

function renderAiSummary(r) {
    const card = document.getElementById('summaryCard');
    const content = document.getElementById('summaryContent');
    if (!card || !content) return;
    card.style.display = 'block';
    if (r && r.ai_summary) {
        _renderSummaryInto(content, r.ai_summary);
    } else {
        _renderSummaryError(content, (r && r.ai_summary_error) ? r.ai_summary_error : 'AI summary unavailable.');
    }
}

async function retryAiSummary() {
    const btn = document.getElementById('summaryRetryBtn');
    const content = document.getElementById('summaryContent');
    if (btn) { btn.disabled = true; btn.textContent = 'Generating (about 30s)...'; }
    try {
        const resp = await fetch('/api/ai_summary', { method: 'POST' });
        const data = await resp.json();
        if (data.ok && data.summary) {
            _renderSummaryInto(content, data.summary);
        } else {
            _renderSummaryError(content, data.error || 'AI summary unavailable.');
        }
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
    }
}
"""