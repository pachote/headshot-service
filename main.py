import os
import io
import json
import zipfile
import logging
import smtplib
import boto3
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
import stripe
import fal_client
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ENV / CONFIG
# ---------------------------------------------------------------------------
FAL_KEY           = os.environ["FAL_KEY"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
R2_ACCESS_KEY     = os.environ["R2_ACCESS_KEY"]
R2_SECRET_KEY     = os.environ["R2_SECRET_KEY"]
R2_ENDPOINT       = os.environ["R2_ENDPOINT"]
R2_BUCKET         = "headshot-service"
GMAIL_USER        = os.environ["GMAIL_USER"]
GMAIL_PASS        = os.environ["GMAIL_PASS"]
APP_URL           = os.environ.get("APP_URL", "https://headshot-service.up.railway.app")

os.environ["FAL_KEY"] = FAL_KEY
stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# PRODUCT CATALOG  (populated at startup by _bootstrap_stripe_products)
# ---------------------------------------------------------------------------
NICHES = {
    "neuro": {
        "name": "AI Headshots for Neuro & BCI Researchers",
        "prompt": "professional headshot, neuro researcher, clean lab background, confident, sharp",
        "price_cents": 4900,
    },
    "founder": {
        "name": "AI Headshots for Tech Founders",
        "prompt": "professional headshot, tech founder, modern office blur background, confident expression",
        "price_cents": 4900,
    },
    "creator": {
        "name": "AI Headshots for Musicians & Creators",
        "prompt": "professional headshot, creative professional, artistic background, expressive",
        "price_cents": 4900,
    },
}

# Hardcoded Stripe price IDs (pre-created 2026-05-22)
PRICE_TO_NICHE: dict[str, str] = {
    "price_1TZv2iIWH9q4fDUwaVK46uBr": "neuro",
    "price_1TZv2jIWH9q4fDUw5pd5pwQU": "founder",
    "price_1TZv2jIWH9q4fDUw3E3hYyve": "creator",
}
NICHE_TO_PRICE: dict[str, str] = {
    "neuro":   "price_1TZv2iIWH9q4fDUwaVK46uBr",
    "founder": "price_1TZv2jIWH9q4fDUw5pd5pwQU",
    "creator": "price_1TZv2jIWH9q4fDUw3E3hYyve",
}


def _bootstrap_stripe_products():
    """Prices are pre-created. Just log confirmation."""
    log.info(f"Stripe price map loaded: {NICHE_TO_PRICE}")


def _bootstrap_stripe_webhook():
    """Webhook is pre-registered. Return env var secret."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    log.info(f"Stripe webhook secret loaded from env: {'yes' if secret else 'MISSING'}")
    return secret


# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------
app = FastAPI(title="AI Headshot Service")


@app.on_event("startup")
async def startup():
    log.info("Bootstrapping Stripe products...")
    _bootstrap_stripe_products()
    log.info("Bootstrapping Stripe webhook...")
    global STRIPE_WEBHOOK_SECRET
    secret = _bootstrap_stripe_webhook()
    if secret:
        STRIPE_WEBHOOK_SECRET = secret
    _ensure_r2_bucket()
    log.info("Startup complete.")


# ---------------------------------------------------------------------------
# R2
# ---------------------------------------------------------------------------
def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def _ensure_r2_bucket():
    s3 = _r2_client()
    try:
        s3.head_bucket(Bucket=R2_BUCKET)
        log.info(f"R2 bucket '{R2_BUCKET}' exists.")
    except Exception:
        s3.create_bucket(Bucket=R2_BUCKET)
        log.info(f"Created R2 bucket '{R2_BUCKET}'.")


def _upload_zip_to_r2(zip_bytes: bytes, filename: str) -> str:
    s3 = _r2_client()
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=filename,
        Body=zip_bytes,
        ContentType="application/zip",
    )
    # Generate presigned URL (7 days)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": filename},
        ExpiresIn=604800,
    )
    return url


# ---------------------------------------------------------------------------
# FAL.ai — generate 20 images (5 calls x 4 images)
# ---------------------------------------------------------------------------
def _generate_headshots(prompt: str) -> list[bytes]:
    full_prompt = f"{prompt}, ultra realistic, 8k, professional photography"
    image_bytes_list: list[bytes] = []

    for batch_idx in range(5):
        log.info(f"FAL batch {batch_idx + 1}/5 ...")
        result = fal_client.run(
            "fal-ai/flux/dev",
            arguments={
                "prompt": full_prompt,
                "image_size": "portrait_4_3",
                "num_images": 4,
                "num_inference_steps": 28,
            },
        )
        images = result.get("images", [])
        for img in images:
            url = img["url"] if isinstance(img, dict) else img
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            image_bytes_list.append(resp.content)
            log.info(f"  Downloaded image {len(image_bytes_list)}/20")

    return image_bytes_list


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------
def _build_zip(image_bytes_list: list[bytes], niche: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, data in enumerate(image_bytes_list, 1):
            zf.writestr(f"headshot_{niche}_{idx:02d}.jpg", data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------
def _send_email(to_email: str, niche: str, download_url: str):
    niche_info = NICHES[niche]
    subject = f"Your AI Headshots are ready — {niche_info['name']}"
    body = f"""Hi there,

Your 20 AI-generated headshots are ready for download!

Download link (valid for 7 days):
{download_url}

Pack: {niche_info['name']}
Images: 20 high-resolution portraits (8K, portrait 4:3)

Enjoy your headshots! If you have any questions, reply to this email.

— NIRA AI Headshot Service
"""
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, to_email, msg.as_bytes())
    log.info(f"Email sent to {to_email}")


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    links = ""
    for key, info in NICHES.items():
        links += f'<li><a href="/checkout/{key}">{info["name"]} — $49</a></li>\n'
    return f"""<!DOCTYPE html>
<html>
<head><title>AI Headshot Service</title>
<style>
  body {{ font-family: sans-serif; max-width: 700px; margin: 60px auto; padding: 0 20px; background: #0d0d0d; color: #eee; }}
  h1 {{ color: #00ffcc; }} a {{ color: #00bfff; }}
  ul {{ line-height: 2; font-size: 1.1em; }}
</style>
</head>
<body>
<h1>AI Headshot Service</h1>
<p>Professional AI-generated headshots delivered to your inbox in minutes. 20 images per pack. $49 one-time.</p>
<ul>
{links}
</ul>
<p style="color:#888;font-size:.9em;">Powered by FLUX + Cloudflare R2. Instant delivery via email.</p>
</body>
</html>"""


@app.get("/checkout/{product_key}")
async def checkout(product_key: str):
    if product_key not in NICHES:
        raise HTTPException(status_code=404, detail="Product not found")

    price_id = NICHE_TO_PRICE.get(product_key)
    if not price_id:
        raise HTTPException(status_code=503, detail="Price not initialized yet")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="payment",
        success_url=f"{APP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/",
        metadata={"niche": product_key},
    )
    return RedirectResponse(url=session.url, status_code=303)


@app.get("/success", response_class=HTMLResponse)
async def success():
    return """<!DOCTYPE html>
<html>
<head><title>Payment Successful</title>
<style>body{{font-family:sans-serif;max-width:600px;margin:80px auto;text-align:center;background:#0d0d0d;color:#eee;}}
h1{{color:#00ffcc;}}</style></head>
<body>
<h1>Payment received!</h1>
<p>Your 20 AI headshots are being generated right now.<br>
Check your inbox in the next 5-10 minutes.</p>
</body>
</html>"""


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except (stripe.error.SignatureVerificationError, ValueError) as e:
        log.error(f"Webhook signature invalid: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        log.error(f"Webhook parse error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        customer_email = session_obj.get("customer_details", {}).get("email") or session_obj.get("customer_email")
        niche = session_obj.get("metadata", {}).get("niche")

        if not customer_email or not niche:
            log.error(f"Missing email or niche in session: {session_obj.get('id')}")
            return Response(content="ok", status_code=200)

        log.info(f"Checkout complete: email={customer_email} niche={niche}")

        try:
            # 1. Generate
            image_bytes_list = _generate_headshots(NICHES[niche]["prompt"])

            # 2. ZIP
            zip_bytes = _build_zip(image_bytes_list, niche)
            log.info(f"ZIP built: {len(zip_bytes):,} bytes ({len(image_bytes_list)} images)")

            # 3. Upload to R2
            safe_email = customer_email.replace("@", "_at_").replace(".", "_")
            filename = f"headshots_{niche}_{safe_email}.zip"
            download_url = _upload_zip_to_r2(zip_bytes, filename)
            log.info(f"Uploaded to R2: {filename}")

            # 4. Email
            _send_email(customer_email, niche, download_url)

        except Exception as e:
            log.error(f"Pipeline error for {customer_email}: {e}", exc_info=True)
            # Return 200 to prevent Stripe retrying — log the error
            return Response(content="pipeline_error", status_code=200)

    return Response(content="ok", status_code=200)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "products_initialized": len(NICHE_TO_PRICE),
        "niches": list(NICHES.keys()),
    }
