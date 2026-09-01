import os
import re
import json
import time
import random
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from playwright.sync_api import sync_playwright

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

ALERT_THRESHOLD_PCT = 15   
HEARTBEAT_DAYS = 5         

STATE_FILE = "tracker_state.json"

VARIANTS = [
    {
        "id": "s26ultra_us",
        "label": "Galaxy S26 Ultra 256GB (US)",
        "currency": "$",
        "url": "https://www.amazon.com/Samsung-Unlocked-Smartphone-Charging-Warranty/dp/B0G4SWDH8P",
        "launch_price": 1299.99,
    },
    {
        "id": "s26_in",
        "label": "Galaxy S26 256GB (India)",
        "currency": "\u20b9",
        "url": "https://www.amazon.in/Samsung-Creative-ProVisual-Customized-Processor/dp/B0GL8BF2X2",
        "launch_price": 87999.0,
    },
    {
        "id": "s26plus_in",
        "label": "Galaxy S26+ 256GB (India)",
        "currency": "\u20b9",
        "url": "https://www.amazon.in/Samsung-Storage-Creative-Wireless-Charging/dp/B0GL8J486T",
        "launch_price": 119999.0,
    },
    {
        "id": "s26ultra_in",
        "label": "Galaxy S26 Ultra 256GB (India)",
        "currency": "\u20b9",
        "url": "https://www.amazon.in/Samsung-Storage-Privacy-Creative-Snapdragon/dp/B0GL85WGTZ",
        "launch_price": 139999.0,
    },
]

BUYBOX_CONTAINERS = ["#corePriceDisplay_desktop_feature_div", "#corePrice_feature_div", "#apex_desktop"]
PRICE_SELECTOR = ".a-price-whole"

MAX_PLAUSIBLE_INCREASE_PCT = 20  
MAX_PLAUSIBLE_DECREASE_PCT = 70   

BLOCK_INDICATORS = [
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
]


MIN_DELAY_BETWEEN_REQUESTS = 4
MAX_DELAY_BETWEEN_REQUESTS = 10

DEFAULT_STATE = {"last_heartbeat_date": None, "alerted_variants": []}


def load_state():
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        state.setdefault("last_heartbeat_date", None)
        state.setdefault("alerted_variants", [])
        return state
    except (json.JSONDecodeError, OSError) as e:
        print(f"State file unreadable ({e}), starting fresh.")
        return dict(DEFAULT_STATE)


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"Could not save state file: {e}")


def should_send_heartbeat(state):
    if state["last_heartbeat_date"] is None:
        return True
    last = datetime.strptime(state["last_heartbeat_date"], "%Y-%m-%d").date()
    return datetime.today().date() >= last + timedelta(days=HEARTBEAT_DAYS)

def clean_price_text(raw_text):
    digits = re.sub(r"[^\d.]", "", raw_text)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def scrape_with_selector(page, label):
    for container in BUYBOX_CONTAINERS:
        try:
            scope = page.locator(container)
            if scope.count() == 0:
                continue
            scope.first.wait_for(timeout=5000)
            raw_text = scope.first.locator(PRICE_SELECTOR).first.inner_text(timeout=5000)
            price = clean_price_text(raw_text)
            if price is not None:
                return price, True  
        except Exception:
            continue

    try:
        page.wait_for_selector(PRICE_SELECTOR, timeout=8000)
        raw_text = page.locator(PRICE_SELECTOR).first.inner_text()
        price = clean_price_text(raw_text)
        return price, False 
    except Exception:
        return None, False


def is_plausible_price(price, launch_price, high_confidence):
    if price is None or launch_price is None:
        return False
    pct_change = 100 * (price - launch_price) / launch_price
    max_up = MAX_PLAUSIBLE_INCREASE_PCT if high_confidence else 10
    max_down = MAX_PLAUSIBLE_DECREASE_PCT if high_confidence else 40
    return -max_down <= pct_change <= max_up


GROQ_MODEL = "openai/gpt-oss-120b"  
PRICE_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def extract_price_loosely(text):
    cleaned = text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
        price = parsed.get("price")
        if price is not None:
            return float(price)
    except Exception:
        pass

    price_match = re.search(r'"?price"?\s*[:=]\s*(' + PRICE_NUMBER_RE.pattern + r')', cleaned, re.IGNORECASE)
    if price_match:
        num = price_match.group(1).replace(",", "")
        try:
            return float(num)
        except ValueError:
            pass

    generic_match = PRICE_NUMBER_RE.search(cleaned)
    if generic_match:
        num = generic_match.group(0).replace(",", "")
        try:
            return float(num)
        except ValueError:
            pass

    return None


def scrape_with_groq_fallback(page, label):
    if not GROQ_AVAILABLE or not os.environ.get("GROQ_API_KEY"):
        return None 

    try:
        body_text = page.inner_text("body")
        snippet = body_text[:6000]  

        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract product prices from raw web page text. "
                        "Reply with JSON only, no other text: "
                        '{"price": <number or null>}. '
                        "The number must be the main purchase price only, "
                        "with no currency symbols or commas."
                    ),
                },
                {"role": "user", "content": f"Page text:\n\n{snippet}"},
            ],
            temperature=0,
            max_tokens=200,
        )
        content = response.choices[0].message.content or ""
        return extract_price_loosely(content)
    except Exception as e:
        print(f"  Groq fallback error: {e}")
        return None


def is_blocked_page(page):
    try:
        text = page.inner_text("body").lower()
        return any(phrase in text for phrase in BLOCK_INDICATORS)
    except Exception:
        return False


def scrape_price(url, label, launch_price):
    price, source = None, "failed"
    browser = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as e:
                print(f"  {label}: could not launch browser ({e})")
                return None, "failed"

            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 768},
                    locale="en-US",
                )

                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = context.new_page()
                page.goto(url, wait_until="load", timeout=60000)

                if is_blocked_page(page):
                    print(f"  {label}: blocked by Amazon (CAPTCHA/robot check)")
                    return None, "blocked"

                selector_price, high_confidence = scrape_with_selector(page, label)
                if selector_price is not None and is_plausible_price(selector_price, launch_price, high_confidence):
                    price, source = selector_price, "selector"
                else:
                    if selector_price is not None:
                        print(f"  {label}: selector found {selector_price} but it's implausible vs launch price, discarding")
                    groq_price = scrape_with_groq_fallback(page, label)
                    if groq_price is not None and is_plausible_price(groq_price, launch_price, True):
                        price, source = groq_price, "groq"
                    elif groq_price is not None:
                        print(f"  {label}: groq found {groq_price} but it's implausible vs launch price, discarding")
                        price, source = None, "implausible"
                    else:
                        price, source = None, "failed"
            except Exception as e:
                print(f"  {label}: page load/extract error ({e})")
                price, source = None, "failed"
    except Exception as e:
        print(f"  {label}: Playwright error ({e})")
        return None, "failed"
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return price, source



def send_email(subject, html_body):
    sender_email = os.environ.get("GMAIL_ADDRESS")
    receiver_raw = os.environ.get("RECEIVER_EMAIL", sender_email)
    app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not sender_email or not app_password:
        print("Email not sent: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set.")
        return

    receiver_list = [addr.strip() for addr in receiver_raw.split(",") if addr.strip()]
    if not receiver_list:
        print("Email not sent: RECEIVER_EMAIL is empty after parsing.")
        return

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = ", ".join(receiver_list)  
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, receiver_list, msg.as_string())  # actual send needs a list
        server.quit()
        print(f"Email sent to {len(receiver_list)} recipient(s): {subject}")
    except Exception as e:
        print(f"Email failed to send: {e}")


def build_status_table_html(results):
    rows = ""
    for r in results:
        v = r["variant"]
        price = r["price"]
        price_str = f"{v['currency']}{price:,.2f}" if price is not None else "Failed to fetch"
        discount_str = f"{r['discount_pct']:.1f}%" if r["discount_pct"] is not None else "\u2014"
        alert_flag = "[DEAL]" if r["is_alert"] else ""
        rows += f"""
        <tr>
            <td>{v['label']} {alert_flag}</td>
            <td>{price_str}</td>
            <td>{v['currency']}{v['launch_price']:,.2f}</td>
            <td>{discount_str}</td>
            <td><a href="{v['url']}">View</a></td>
        </tr>"""

    return f"""
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; font-family: sans-serif;">
        <tr style="background:#f0f0f0;">
            <th>Variant</th>
            <th>Current Price</th>
            <th>Launch Price</th>
            <th>Discount</th>
            <th>Link</th>
        </tr>
        {rows}
    </table>
    """


def main():
    state = load_state()
    results = []
    newly_alerted = []
    fail_count = 0

    groq_ready = GROQ_AVAILABLE and bool(os.environ.get("GROQ_API_KEY"))
    print(f"Tracking {len(VARIANTS)} variants (Groq fallback: {'on' if groq_ready else 'off'})")

    for i, variant in enumerate(VARIANTS):
        if i > 0:
            delay = random.uniform(MIN_DELAY_BETWEEN_REQUESTS, MAX_DELAY_BETWEEN_REQUESTS)
            time.sleep(delay)

        try:
            price, source = scrape_price(variant["url"], variant["label"], variant["launch_price"])
        except Exception as e:
            print(f"  {variant['label']}: unexpected error ({e})")
            price, source = None, "failed"

        discount_pct = None
        is_alert = False

        if price is not None:
            discount_pct = 100 * (variant["launch_price"] - price) / variant["launch_price"]
            already_alerted = variant["id"] in state["alerted_variants"]
            if discount_pct >= ALERT_THRESHOLD_PCT and not already_alerted:
                is_alert = True
                newly_alerted.append(variant["id"])
            tag = " <- DEAL" if is_alert else ""
            print(f"  {variant['label']}: {variant['currency']}{price:,.2f} ({source}, {discount_pct:+.1f}%){tag}")
        else:
            fail_count += 1
            print(f"  {variant['label']}: no price ({source})")

        results.append({
            "variant": variant,
            "price": price,
            "discount_pct": discount_pct,
            "is_alert": is_alert,
        })

    if fail_count == len(VARIANTS):
        print("All variants failed to scrape — skipping email, nothing to report.")
        save_state(state)
        return

    try:
        force_send = os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes")

        if newly_alerted:
            alert_results = [r for r in results if r["variant"]["id"] in newly_alerted]
            subject = "\U0001f6a8 ALERT: Samsung S26 Price Drop Detected!"
            body = f"""
            <h3>Samsung S26 Tracker \u2014 Price Drop Alert</h3>
            <p>The following variant(s) have dropped {ALERT_THRESHOLD_PCT}% or more below their launch price:</p>
            {build_status_table_html(alert_results)}
            <br/>
            <h4>Full status (all tracked variants):</h4>
            {build_status_table_html(results)}
            <p><small>Automated tracker.</small></p>
            """
            send_email(subject, body)
            state["alerted_variants"].extend(newly_alerted)

        elif should_send_heartbeat(state) or force_send:
            reason = "Forced test send (FORCE_SEND=true)" if force_send and not should_send_heartbeat(state) else f"Routine {HEARTBEAT_DAYS}-Day Report"
            subject = "\U0001f4ca Status Update: Samsung S26 Price Report" + (" [TEST]" if force_send else "")
            body = f"""
            <h3>Samsung S26 Tracker \u2014 {reason}</h3>
            <p>No new price drops past the {ALERT_THRESHOLD_PCT}% threshold since the last alert.</p>
            {build_status_table_html(results)}
            <p><small>Automated tracker.</small></p>
            """
            send_email(subject, body)
            if not force_send:
                state["last_heartbeat_date"] = str(datetime.today().date())
            else:
                print("  (test send — heartbeat timer not updated)")
        else:
            print("No new deal, heartbeat not due. No email sent.")
    finally:
        save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: tracker run failed: {e}")
        raise