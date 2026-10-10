"""Small self-contained style for the loopback owner console."""
import html

STYLE = """
:root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; color: #e8edf9; background: radial-gradient(circle at 15% 0%, #172749 0, transparent 42%), radial-gradient(circle at 90% 20%, #172339 0, transparent 38%), #0b1020; }
main { max-width: 1060px; margin: 0 auto; padding: clamp(24px, 5vw, 64px) 22px 80px; }
header { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; margin-bottom: 30px; }
.brand { display: inline-flex; align-items: center; gap: 12px; color: #8eb4ff; font-size: 13px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.logo { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 12px; background: linear-gradient(140deg, #3776f6, #7456e6); color: white; font-size: 18px; box-shadow: 0 10px 25px #172d6b77; }
h1 { margin: 17px 0 8px; font-size: clamp(29px, 4vw, 42px); letter-spacing: -.035em; line-height: 1.1; }
h2 { margin: 0; font-size: 19px; letter-spacing: -.015em; }
p { line-height: 1.6; }
.lead, .muted { color: #a4b1c9; }
.lead { margin: 0; font-size: 15px; }
.local { color: #a7e4c0; border: 1px solid #3b7055; background: #12342599; padding: 8px 11px; border-radius: 999px; white-space: nowrap; font-size: 12px; font-weight: 700; }
.card { background: #141d31ed; border: 1px solid #2a3854; border-radius: 20px; padding: clamp(20px, 3vw, 30px); box-shadow: 0 18px 50px #02061355; margin: 20px 0; }
.section-top { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.section-top p { margin: 5px 0 0; font-size: 13px; color: #a4b1c9; }
.count { display: inline-flex; align-items: center; justify-content: center; min-width: 28px; height: 28px; padding: 0 8px; color: #a9c3ff; background: #24375b; border: 1px solid #355186; border-radius: 999px; font-size: 12px; font-weight: 700; }
.empty { border: 1px dashed #394967; background: #10182a; border-radius: 14px; padding: 23px; color: #a4b1c9; }
.empty strong { display: block; color: #e8edf9; margin-bottom: 4px; }
.list { display: grid; gap: 10px; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 20px; padding: 16px 18px; border: 1px solid #2b3952; border-radius: 14px; background: #10192b; }
.row-info { min-width: 0; overflow-wrap: anywhere; }
.row-title { font-size: 15px; font-weight: 700; }
.meta { color: #a4b1c9; font-size: 12px; margin-top: 6px; line-height: 1.5; }
code { color: #b3c9ff; font: 12px ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.pill { display: inline-block; background: #213153; color: #b6cdff; border-radius: 7px; padding: 4px 7px; margin: 3px 4px 3px 0; font: 12px ui-monospace, Consolas, monospace; }
.pill.green { color: #98e6b9; background: #164432; }
.pill.dim { color: #91a1ba; background: #263045; }
.actions { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
button, .button { appearance: none; border: 1px solid #425679; color: #e8edf9; background: #23324e; border-radius: 9px; padding: 10px 14px; font: 700 13px inherit; font-family: inherit; font-size: 13px; font-weight: 700; cursor: pointer; text-decoration: none; white-space: nowrap; }
button:hover, .button:hover { background: #304365; }
button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid #93b4ff; outline-offset: 3px; }
.primary { border-color: #5b89ef; background: #3669d8; color: #fff; }
.primary:hover { background: #477aeb; }
.danger { border-color: #794251; background: #3c202e; color: #ffd4dc; }
.danger:hover { background: #5a2939; }
.link { color: #a8c4ff; text-decoration: none; font-size: 13px; font-weight: 700; }
.link:hover { text-decoration: underline; }
.note { color: #9caac4; font-size: 12px; margin: 18px 0 0; }
.details { margin-top: 18px; }
.details summary { color: #b2c5ed; cursor: pointer; font-weight: 700; }
.details .list { margin-top: 13px; }
.approval { max-width: 650px; }
.field { padding: 14px 0; border-top: 1px solid #2b3952; }
.field-label { display: block; color: #91a2bf; font-size: 12px; text-transform: uppercase; letter-spacing: .07em; margin-bottom: 6px; }
.approval .actions { padding-top: 18px; }
@media(max-width: 670px) { header, .row { flex-direction: column; align-items: stretch; } .local { align-self: flex-start; } .actions form, .actions button { width: 100%; } }
"""


def render(title: str, body: str, referrer: str = "same-origin") -> str:
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='referrer' content='{html.escape(referrer, quote=True)}'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
            f"<main>{body}</main></body></html>")
