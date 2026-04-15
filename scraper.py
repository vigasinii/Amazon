"""
PriceIQ — Single Product Scraper
=================================
Scrapes ONE product from Amazon and outputs scraped_data.json
for the dashboard (index.html) to read.

Usage:
    python scraper.py
      → prompts you for ASIN + pricing inputs interactively

    python scraper.py B08C1W5N87 39.99 16.00 24.00 34.99
      → ASIN  my_price  cost  floor  map_price (all as args)
"""

import requests, json, re, sys, time, os
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# Fix Windows console encoding (cp1252 can't handle arrows/emoji in print)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

SYPHOON_KEY = "fUG5oU4zytxScq4VW7pH"
SYPHOON_URL = "https://api.syphoon.com/"
OUTPUT_FILE = "scraped_data.json"
DELAY       = 1.5

# ── FETCH ─────────────────────────────────────────────────────
def fetch(url):
    for attempt in range(3):
        try:
            r = requests.post(SYPHOON_URL,
                json={"url": url, "key": SYPHOON_KEY, "method": "GET"},
                headers={"Content-Type": "application/json"}, timeout=30)
            if r.status_code == 200:
                if len(r.text) < 3000 and "Page Not Found" in r.text:
                    print(f"    ⚠ Bot-blocked")
                    return None
                return r.text
            elif r.status_code == 429:
                print("    ⚠ Rate limited, waiting 5s...")
                time.sleep(5)
            elif r.status_code == 401:
                print("    ✗ Invalid API key"); sys.exit(1)
            else:
                print(f"    ✗ HTTP {r.status_code}")
                return None
        except Exception as e:
            print(f"    ✗ {e}")
            if attempt < 2: time.sleep(2)
    return None

# ── PARSERS — PRODUCT PAGE ────────────────────────────────────

def get_price(soup):
    # Primary: a-price span (correctly split whole + fraction)
    price_el = soup.find("span", class_="a-price")
    if price_el:
        whole = price_el.find("span", class_="a-price-whole")
        frac  = price_el.find("span", class_="a-price-fraction")
        if whole:
            try:
                w = whole.text.strip().replace(",", "").rstrip(".")
                f = frac.text.strip() if frac else "00"
                return float(f"{w}.{f}")
            except: pass
    # Fallback: known IDs
    for pid in ["apexPriceToPay", "priceblock_ourprice", "priceblock_dealprice"]:
        el = soup.find(id=pid)
        if el:
            m = re.search(r"\$([\d,]+\.[\d]{2})", el.text)
            if m: return float(m.group(1).replace(",", ""))
    # Fallback: JSON in page script
    for s in soup.find_all("script"):
        t = s.string or ""
        m = re.search(r'"priceAmount"\s*:\s*([\d.]+)', t)
        if m: return float(m.group(1))
    return None

def get_title(soup):
    el = soup.find(id="productTitle")
    if el: return el.text.strip()
    og = soup.find("meta", property="og:title")
    if og: return og.get("content", "").strip()
    return None

def get_brand(soup):
    el = soup.find(id="bylineInfo")
    if el:
        return el.text.strip()\
            .replace("Visit the ", "").replace(" Store", "")\
            .replace("Brand: ", "").strip()
    el = soup.find(id="brand")
    if el: return el.text.strip()
    for row in soup.find_all("tr"):
        th = row.find("th"); td = row.find("td")
        if th and td and "brand" in th.text.lower():
            return td.text.strip()
    return None

def get_category(soup):
    bc = soup.find(id="wayfinding-breadcrumbs_feature_div")
    if bc:
        items = [a.text.strip() for a in bc.find_all("a") if a.text.strip()]
        if items: return " > ".join(items)
    el = soup.find(id="nav-subnav")
    if el:
        a = el.find("a", class_="nav-a")
        if a: return a.text.strip()
    return "Unknown"

def get_rating(soup):
    for el in soup.find_all("span", class_="a-icon-alt"):
        text = el.text.strip()
        if "out of 5" in text:
            m = re.search(r"([\d.]+)\s*out of", text)
            if m: return float(m.group(1))
    return None

def get_reviews(soup):
    el = soup.find(id="acrCustomerReviewText") or \
         soup.select_one("span[data-hook='total-review-count']")
    if el:
        m = re.search(r"([\d,]+)", el.text)
        if m: return int(m.group(1).replace(",", ""))
    return None

def get_stock(soup):
    el = soup.find(id="availability")
    if el:
        t = el.text.strip().lower()
        if "in stock"    in t: return "in"
        if "only"        in t: return "low"
        if "unavailable" in t or "out of stock" in t: return "oos"
    if soup.find(id="add-to-cart-button"): return "in"
    return "oos"

def get_buybox_seller(soup):
    """Infer buybox seller — JS-rendered div is empty in static HTML"""
    has_bb = bool(soup.find(id="add-to-cart-button") or soup.find(id="buy-now-button"))
    seller = None
    body   = soup.get_text(" ")
    if "shipped by Amazon" in body or "Fulfilled by Amazon" in body:
        seller = "Amazon"
    else:
        for sel in ["#sellerProfileTriggerId", ".tabular-buybox-text a", "#merchant-info a"]:
            el = soup.select_one(sel)
            if el and el.text.strip():
                seller = el.text.strip(); break
    return has_bb, seller

def get_seller_count(soup):
    for pat in [r"(\d+)\s+new", r"(\d+)\s+offer", r"See all (\d+)"]:
        el = soup.find(string=re.compile(pat, re.I))
        if el:
            m = re.search(pat, str(el), re.I)
            if m: return int(m.group(1))
    a = soup.find("a", href=re.compile(r"offer-listing"))
    if a:
        m = re.search(r"(\d+)", a.text)
        if m: return int(m.group(1))
    return 0

def get_images(soup):
    images = []
    for pid in ["landingImage", "imgBlkFront"]:
        el = soup.find(id=pid)
        if el:
            url = el.get("data-old-hires") or el.get("src")
            if url and url not in images: images.append(url)
    for s in soup.find_all("script"):
        t = s.string or ""
        for m in re.finditer(r'"hiRes"\s*:\s*"(https://[^"]+)"', t):
            url = m.group(1)
            if url not in images: images.append(url)
    og = soup.find("meta", property="og:image")
    if og and og.get("content") and og["content"] not in images:
        images.append(og["content"])
    return images[:8]

def get_bullets(soup):
    bullets = []
    ul = soup.find(id="feature-bullets") or \
         soup.find("div", id="featurebullets_feature_div")
    if ul:
        for li in ul.find_all("li"):
            text = li.text.strip()
            if text and len(text) > 5 and "›" not in text:
                bullets.append(text)
    return bullets[:10]

def get_description(soup):
    for pid in ["productDescription", "aplus_feature_div", "bookDescription_feature_div"]:
        el = soup.find(id=pid)
        if el:
            text = el.get_text(separator=" ", strip=True)
            if len(text) > 30: return text[:1000]
    return None

def get_asin_confirmed(soup):
    for row in soup.find_all("tr"):
        th = row.find("th"); td = row.find("td")
        if th and td and "asin" in th.text.lower():
            return td.text.strip()
    m = re.search(r'"ASIN"\s*:\s*"([A-Z0-9]{10})"', str(soup))
    if m: return m.group(1)
    return None

# ── PARSERS — OFFERS PAGE ─────────────────────────────────────
# NOTE: Amazon loads offer listings via JavaScript.
# Syphoon fetches static HTML only, so the AOD offer divs will be empty.
# The generic fallback scans for any price spans as best-effort.

def get_competitors(soup, my_price):
    competitors = []

    # Method 1: AOD divs (JS-rendered — usually empty with Syphoon)
    for sec in soup.find_all("div", id=re.compile(r"aod-offer")):
        comp = _parse_aod_section(sec, my_price)
        if comp: competitors.append(comp)

    # Method 2: OLP rows
    if not competitors:
        for row in soup.find_all("div", class_=re.compile(r"olpOffer")):
            comp = _parse_olp_row(row, my_price)
            if comp: competitors.append(comp)

    # Method 3: Generic regex scan
    if not competitors:
        competitors = _parse_generic(soup, my_price)

    competitors.sort(key=lambda x: x["price"])
    return competitors

def _parse_aod_section(sec, my_price):
    price_el = sec.find("span", class_=re.compile(r"a-price(?!\s*-strike)"))
    if not price_el: return None
    whole = price_el.find("span", class_="a-price-whole")
    frac  = price_el.find("span", class_="a-price-fraction")
    try:
        w = whole.text.strip().replace(",", "").rstrip(".") if whole else "0"
        f = frac.text.strip() if frac else "00"
        price = float(f"{w}.{f}")
    except: return None
    if price <= 0: return None

    seller = "Unknown Seller"
    for sel in [".a-profile-name", "[id*='sellerProfileTrigger']", ".aod-seller-name span"]:
        el = sec.select_one(sel)
        if el and el.text.strip(): seller = el.text.strip(); break

    condition = "New"
    for sel in ["span[id*='condition']", ".aod-condition-name"]:
        el = sec.select_one(sel)
        if el and el.text.strip():
            t = el.text.strip()
            if any(c in t.lower() for c in ["new", "used", "refurb"]):
                condition = t; break

    shipping = "Unknown"
    for sel in [".aod-ship-charge", "[id*='delivery']"]:
        el = sec.select_one(sel)
        if el and el.text.strip():
            t = el.text.strip()
            if any(k in t.lower() for k in ["ship", "deliver", "free", "$"]):
                shipping = t[:60]; break

    return {"seller": seller, "price": price, "condition": condition,
            "shipping": shipping, "is_cheaper": price < my_price}

def _parse_olp_row(row, my_price):
    price_el = row.find("span", class_=re.compile(r"olpOfferPrice|a-color-price"))
    if not price_el: return None
    m = re.search(r"\$([\d,]+\.[\d]{2})", price_el.text)
    if not m: return None
    price = float(m.group(1).replace(",", ""))
    seller_el = row.find("span", class_="a-profile-name")
    seller = seller_el.text.strip() if seller_el else "Marketplace Seller"
    cond_el = row.find("span", class_=re.compile(r"olpCondition"))
    condition = cond_el.text.strip() if cond_el else "New"
    ship_el = row.find("p", class_=re.compile(r"olpShipping"))
    shipping = ship_el.text.strip()[:60] if ship_el else "Unknown"
    return {"seller": seller, "price": price, "condition": condition,
            "shipping": shipping, "is_cheaper": price < my_price}

def _parse_generic(soup, my_price):
    competitors = []
    seen = set()
    pat = re.compile(r'a-price-whole">([\d,]+)<[^>]*>.*?a-price-fraction">([\d]+)', re.DOTALL)
    for w, f in pat.findall(str(soup))[:15]:
        try:
            price = float(w.replace(",", "") + "." + f)
            if price > 0 and price not in seen:
                seen.add(price)
                competitors.append({"seller": "Marketplace Seller", "price": price,
                                    "condition": "New", "shipping": "Unknown",
                                    "is_cheaper": price < my_price})
        except: pass
    return sorted(competitors, key=lambda x: x["price"])

# ── SEARCH-BASED COMPETITOR SCRAPER ──────────────────────────
def scrape_competitors(title, brand, my_asin, my_price, max_results=8):
    """
    Search Amazon for competing products.
    Builds a generic category query (strips brand/model words)
    and filters out same-brand/same-family results.
    """
    competitors = []
    if not title:
        return competitors

    brand_lower = (brand or "").lower()

    # Words to strip from the query — brand names, Amazon ecosystem words,
    # model identifiers, and generic filler
    STRIP_WORDS = {
        'with','and','the','for','new','gen','generation','model','release',
        'edition','version','latest','newest','updated','series','plus','pro',
        'max','mini','lite','ultra','amazon','alexa','echo','fire','kindle',
        'prime','smart','voice','remote','control','compatible','works',
        'including','featuring','designed','powered','enabled','built',
        '1st','2nd','3rd','4th','5th','2018','2019','2020','2021','2022',
        '2023','2024','charcoal','black','white','gray','grey','blue','red',
    }
    if brand:
        for bw in brand.lower().split():
            if len(bw) > 2:
                STRIP_WORDS.add(bw)

    # Build category query from meaningful words only
    query_words = []
    for w in title.split():
        cleaned = re.sub(r'[^a-zA-Z0-9]', '', w).lower()
        if cleaned and len(cleaned) > 2 and cleaned not in STRIP_WORDS:
            query_words.append(w)
        if len(query_words) >= 4:
            break

    if not query_words:
        query_words = title.split()[:4]

    query = " ".join(query_words)
    query_enc = requests.utils.quote(query)

    # All distinctive words from title for same-family detection
    distinctive = [re.sub(r'[^a-z0-9]','',w.lower()) for w in title.split()
                   if len(w) > 3 and w.lower() not in {'with','and','the','for','new','this'}]

    print(f"    Query: {query[:60]}")
    print(f"    Filtering out brand '{brand}' and family words: {distinctive[:5]}")

    html = None
    for url in [f"https://www.amazon.com/s?k={query_enc}", f"https://www.amazon.com/s?field-keywords={query_enc}"]:
        print(f"    Trying: {url[:80]}")
        html = fetch(url)
        if html and len(html) > 5000 and 'data-asin' in html:
            print(f"    Got valid search page ({len(html)} bytes)")
            break
        else:
            print(f"    Got: {len(html) if html else 0} bytes")
            html = None
        time.sleep(DELAY)

    if not html:
        print("    Could not get valid search results page")
        return competitors

    soup = BeautifulSoup(html, "html.parser")
    all_cards = soup.select("div[data-asin]")
    print(f"    Found {len(all_cards)} result cards")

    seen_asins  = {my_asin}
    seen_prices = set()

    def is_same_family(comp_name):
        if not comp_name:
            return False
        comp_lower = comp_name.lower()
        # Brand name appears in their title → same brand, skip
        if brand_lower and len(brand_lower) > 2 and brand_lower in comp_lower:
            return True
        # Use only the first 2-3 words of our title as the "product identity"
        # e.g. "Echo Dot" or "Fire TV" — if those appear together, same family
        identity_words = [re.sub(r'[^a-z0-9]','',w.lower()) for w in title.split()[:3]
                         if len(w) > 2 and w.lower() not in {'the','for','and','with','new'}]
        if len(identity_words) >= 2:
            # Both first two identity words must appear in comp name
            if identity_words[0] in comp_lower and identity_words[1] in comp_lower:
                return True
        return False

    for card in all_cards[:max_results + 15]:
        asin = card.get("data-asin", "").strip()
        if not asin or len(asin) != 10 or asin in seen_asins:
            continue
        seen_asins.add(asin)

        # Parse name first for family filtering
        name = None
        for sel in ["h2 a span","h2 span.a-text-normal","h2 span",
                    "[data-cy='title-recipe'] span",".a-size-medium.a-text-normal",
                    ".a-size-base-plus.a-text-normal"]:
            el = card.select_one(sel)
            if el:
                txt = el.get_text(strip=True)
                if txt and len(txt) > 8 and txt.lower() not in ('amazon','sponsored','new'):
                    name = txt
                    break
        if not name or name.startswith('ASIN '):
            # Try fetching the product page for the name as fallback
            try:
                prod_html = fetch(f"https://www.amazon.com/dp/{asin}")
                if prod_html:
                    prod_soup = BeautifulSoup(prod_html, "html.parser")
                    title_el = prod_soup.find(id="productTitle")
                    if title_el:
                        name = title_el.text.strip()[:80]
            except:
                pass
        if not name or name.startswith('ASIN '):
            name = f"ASIN {asin}"

        if is_same_family(name):
            print(f"    Skip {asin}: same family — {name[:50]}")
            continue

        # Price
        price_el = card.find("span", class_="a-price")
        if not price_el:
            print(f"    Skip {asin}: no price — {name[:40]}")
            continue
        whole = price_el.find("span", class_="a-price-whole")
        frac  = price_el.find("span", class_="a-price-fraction")
        try:
            w = whole.text.strip().replace(",","").rstrip(".") if whole else "0"
            f = frac.text.strip() if frac else "00"
            price = float(f"{w}.{f}")
        except:
            continue
        if price <= 0 or price in seen_prices:
            continue
        seen_prices.add(price)

        # Rating
        rating = None
        rating_el = card.find("span", class_="a-icon-alt")
        if rating_el:
            m = re.search(r"([\d.]+)\s*out of", rating_el.text)
            if m:
                try: rating = float(m.group(1))
                except: pass

        # Reviews
        reviews = None
        rev_el = card.select_one("a[href*='customerReviews'] span.a-size-base") or \
                 card.select_one("span.a-size-base + span.a-size-base")
        if rev_el:
            m = re.search(r"([\d,]+)", rev_el.text)
            if m:
                try: reviews = int(m.group(1).replace(",",""))
                except: pass

        # URL — always use clean ASIN-based URL (most reliable)
        url = f"https://www.amazon.com/dp/{asin}"

        print(f"    + Found: ${price} — {name[:50]}")
        competitors.append({
            "asin": asin, "name": name, "seller": "Marketplace Seller",
            "price": price, "condition": "New", "shipping": "Unknown",
            "rating": rating, "reviews": reviews, "url": url,
            "is_cheaper": price < my_price, "comp_type": "brand"
        })

        if len(competitors) >= max_results:
            break

    print(f"    Total competitors found: {len(competitors)}")
    return sorted(competitors, key=lambda x: x["price"])

# ── CATEGORY → ICON ──────────────────────────────────────────
ICONS = {"electronics":"⚡","kitchen":"🍳","sports":"💧","outdoors":"🏕",
         "toys":"🧱","books":"📖","home":"🏠","clothing":"👕",
         "beauty":"💄","automotive":"🚗","garden":"🌿"}

def cat_icon(cat):
    c = cat.lower()
    for k, v in ICONS.items():
        if k in c: return v
    return "📦"

# ── RESELLER COMPETITOR SCRAPER ──────────────────────────────
def scrape_resellers(asin, my_price, max_results=10):
    """
    Scrape other sellers listing the EXACT same ASIN (offer listing page).
    These are true reseller competitors — same product, different price/seller.
    """
    competitors = []
    url = f"https://www.amazon.com/gp/offer-listing/{asin}?condition=new"
    print(f"    Fetching offer listing for {asin}...")
    html = fetch(url)
    if not html:
        print("    ✗ Could not fetch offer listing")
        return competitors

    soup = BeautifulSoup(html, "html.parser")

    # Try AOD sections first
    for sec in soup.find_all("div", id=re.compile(r"aod-offer")):
        comp = _parse_aod_section(sec, my_price)
        if comp:
            comp["asin"] = asin
            comp["name"] = f"Reseller: {comp['seller']}"
            comp["reviews"] = None
            comp["rating"] = None
            comp["url"] = f"https://www.amazon.com/gp/offer-listing/{asin}"
            comp["is_reseller"] = True
            competitors.append(comp)

    # Fallback: OLP rows
    if not competitors:
        for row in soup.find_all("div", class_=re.compile(r"olpOffer")):
            comp = _parse_olp_row(row, my_price)
            if comp:
                comp["asin"] = asin
                comp["name"] = f"Reseller: {comp['seller']}"
                comp["reviews"] = None
                comp["rating"] = None
                comp["url"] = f"https://www.amazon.com/gp/offer-listing/{asin}"
                comp["is_reseller"] = True
                competitors.append(comp)

    # Generic fallback
    if not competitors:
        comps = _parse_generic(soup, my_price)
        for c in comps:
            c["asin"] = asin
            c["name"] = f"Reseller: {c['seller']}"
            c["reviews"] = None
            c["rating"] = None
            c["is_reseller"] = True
        competitors = comps

    print(f"    Found {len(competitors)} resellers")
    return sorted(competitors, key=lambda x: x["price"])[:max_results]



def scrape(asin, my_price=0, cost=0, floor=0, map_price=0, comp_mode="brand"):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    result = {
        "asin": asin, "name": None, "brand": None, "cat": "Unknown",
        "asin_confirmed": None,
        # user-provided pricing
        "my_price": my_price, "cost": cost, "floor": floor, "map": map_price,
        # scraped pricing
        "price": my_price,        # displayed price (my_price if set, else scraped)
        "scraped_price": None,    # actual Amazon buybox price
        "compLow": 0, "compHigh": 0, "compAvg": 0,
        "compOffers": [],
        "sellers": 0,
        # product details
        "stock": "oos", "buybox": False, "buybox_seller": None,
        "rating": None, "review_count": None,
        "images": [], "bullets": [], "description": None,
        # derived
        "gap": 0, "urgency": "low", "img": "📦",
        "last_scraped": now, "scrape_status": "failed"
    }

    # ── 1. Product Page ───────────────────────────────────────
    print(f"\n  → Fetching product page...")
    html = fetch(f"https://www.amazon.com/dp/{asin}")
    if not html:
        print("  ✗ Could not fetch product page")
        return result

    soup = BeautifulSoup(html, "html.parser")

    result["name"]            = get_title(soup)
    result["brand"]           = get_brand(soup)
    result["cat"]             = get_category(soup)
    result["asin_confirmed"]  = get_asin_confirmed(soup)
    result["rating"]          = get_rating(soup)
    result["review_count"]    = get_reviews(soup)
    result["stock"]           = get_stock(soup)
    result["images"]          = get_images(soup)
    result["bullets"]         = get_bullets(soup)
    result["description"]     = get_description(soup)
    result["sellers"]         = get_seller_count(soup)
    result["img"]             = cat_icon(result["cat"])

    scraped_price = get_price(soup)
    if scraped_price:
        result["scraped_price"] = scraped_price
        # Use my_price if provided, otherwise use scraped price
        result["price"] = my_price if my_price > 0 else scraped_price

    bb, bb_seller = get_buybox_seller(soup)
    result["buybox"]        = bb
    result["buybox_seller"] = bb_seller

    print(f"  ✓ {(result['name'] or asin)[:60]}")
    print(f"    Scraped price : ${result['scraped_price']}")
    print(f"    My price      : ${result['price']}")
    print(f"    Stock         : {result['stock']}")
    print(f"    Buy Box       : {result['buybox']} (seller: {result['buybox_seller']})")
    print(f"    Rating        : {result['rating']} ({result['review_count']} reviews)")
    print(f"    Category      : {result['cat']}")
    print(f"    Brand         : {result['brand']}")
    print(f"    Bullets       : {len(result['bullets'])}")
    print(f"    Images        : {len(result['images'])}")

    time.sleep(DELAY)

    # ── 2. Competitors (mode-aware) ───────────────────────────
    competitors = []

    if comp_mode in ("brand", "both"):
        print(f"\n  → [Brand mode] Searching Amazon for similar products...")
        brand_comps = scrape_competitors(
            title    = result["name"],
            brand    = result["brand"],
            my_asin  = asin,
            my_price = result["price"],
            max_results = 8
        )
        for c in brand_comps:
            c["comp_type"] = "brand"
        competitors += brand_comps

    if comp_mode in ("reseller", "both"):
        print(f"\n  → [Reseller mode] Fetching offer listing for {asin}...")
        time.sleep(DELAY)
        reseller_comps = scrape_resellers(asin, result["price"], max_results=10)
        for c in reseller_comps:
            c["comp_type"] = "reseller"
        competitors += reseller_comps

    # Deduplicate by price+name
    seen = set()
    deduped = []
    for c in competitors:
        key = (round(c["price"], 2), c.get("asin",""), c.get("seller",""))
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    competitors = sorted(deduped, key=lambda x: x["price"])

    if competitors:
        result["compOffers"] = competitors
        prices = [c["price"] for c in competitors]
        result["compLow"]  = min(prices)
        result["compHigh"] = max(prices)
        result["compAvg"]  = round(sum(prices) / len(prices), 2)
        result["sellers"]  = len(competitors)
        cheaper = [c for c in competitors if c["is_cheaper"]]
        print(f"    Found {len(competitors)} competitors | low=${result['compLow']} | high=${result['compHigh']}")
        print(f"    {len(cheaper)} cheaper than your price ${result['price']}")
        for c in competitors[:5]:
            tag = "CHEAPER" if c["is_cheaper"] else "higher "
            stars = f"★{c['rating']}" if c["rating"] else "no rating"
            print(f"    [{tag}] ${c['price']:>7.2f}  {stars}  {c['name'][:45]}")
    else:
        print("    No competitors found from search")

    time.sleep(DELAY)

    # ── 3. Derived Fields ─────────────────────────────────────
    if result["price"] > 0 and result["compLow"] > 0:
        result["gap"] = round(
            (result["price"] - result["compLow"]) / result["compLow"] * 100, 1)

    gap, no_bb = result["gap"], not result["buybox"]
    if   no_bb and abs(gap) > 10: result["urgency"] = "critical"
    elif no_bb and abs(gap) > 5:  result["urgency"] = "high"
    elif abs(gap) > 10:           result["urgency"] = "high"
    elif abs(gap) > 5:            result["urgency"] = "medium"
    else:                         result["urgency"] = "low"
    if result["stock"] == "oos":  result["urgency"] = "high"

    result["scrape_status"] = "ok"
    return result

# ── SAVE ──────────────────────────────────────────────────────
def save(product):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Load existing data so we can merge (multi-product support)
    existing_products = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
                existing_products = existing.get("products", [])
        except:
            existing_products = []

    # Replace if ASIN already exists, otherwise append
    found = False
    for i, p in enumerate(existing_products):
        if p.get("asin") == product["asin"]:
            existing_products[i] = product
            found = True
            break
    if not found:
        existing_products.append(product)

    success_count = sum(1 for p in existing_products if p.get("scrape_status") == "ok")
    output = {
        "scraped_at":   ts,
        "total":        len(existing_products),
        "success":      success_count,
        "success_rate": round(success_count / len(existing_products) * 100, 1) if existing_products else 0,
        "products":     existing_products
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Saved → {OUTPUT_FILE} ({len(existing_products)} total products)")

# ── INPUT HELPERS ─────────────────────────────────────────────
def ask(prompt, cast=str, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return cast(raw)
        except:
            print(f"    ✗ Invalid input, try again")

# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    print("\n" + "="*55)
    print("  PriceIQ — Single Product Scraper")
    print("="*55)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('asin',      nargs='?', default=None)
    parser.add_argument('my_price',  nargs='?', type=float, default=0)
    parser.add_argument('cost',      nargs='?', type=float, default=0)
    parser.add_argument('floor',     nargs='?', type=float, default=0)
    parser.add_argument('map_price', nargs='?', type=float, default=0)
    parser.add_argument('--comp-mode', dest='comp_mode', default='brand',
                        choices=['brand','reseller','both'])
    args = parser.parse_args()

    if args.asin:
        asin      = args.asin.strip().upper()
        my_price  = args.my_price
        cost      = args.cost
        floor     = args.floor
        map_price = args.map_price
        comp_mode = args.comp_mode
        print(f"\n  ASIN      : {asin}")
        print(f"  My Price  : ${my_price}")
        print(f"  Cost      : ${cost}")
        print(f"  Floor     : ${floor}")
        print(f"  MAP       : ${map_price}")
        print(f"  Comp Mode : {comp_mode}")
    else:
        print("\n  Enter product details (scraped fields filled automatically)\n")
        asin = ask("ASIN (10 characters)", str).upper()
        if len(asin) != 10:
            print("  ✗ ASIN must be 10 characters"); sys.exit(1)
        my_price  = ask("My price ($)  — your listing price", float, 0)
        cost      = ask("Cost ($)      — what you paid",      float, 0)
        floor     = ask("Floor ($)     — minimum you'll accept", float, 0)
        map_price = ask("MAP ($)       — minimum advertised price", float, 0)
        print("\n  Competitor mode:")
        print("    brand    — similar products from other brands (default)")
        print("    reseller — other sellers listing this exact ASIN")
        print("    both     — brand + reseller combined")
        comp_mode = ask("Comp mode", str, "brand").strip().lower()
        if comp_mode not in ("brand", "reseller", "both"):
            comp_mode = "brand"

    print(f"\n  Scraping {asin}...")
    print("="*55)

    product = scrape(asin, my_price=my_price, cost=cost,
                     floor=floor, map_price=map_price, comp_mode=comp_mode)

    print("\n" + "="*55)
    print("  RESULTS SUMMARY")
    print("="*55)
    print(f"  Product   : {product['name'] or 'N/A'}")
    print(f"  Brand     : {product['brand'] or 'N/A'}")
    print(f"  Category  : {product['cat']}")
    print(f"  Price     : ${product['price']} (scraped: ${product['scraped_price']})")
    print(f"  Stock     : {product['stock']}")
    print(f"  Buy Box   : {'Yes' if product['buybox'] else 'No'} — {product['buybox_seller'] or 'unknown'}")
    rev = f"{product['review_count']:,} reviews" if product['review_count'] else 'N/A'
    print(f"  Rating    : {product['rating']} ({rev})")
    print(f"  Gap       : {product['gap']}%")
    print(f"  Urgency   : {product['urgency'].upper()}")
    print(f"  Comp Mode : {comp_mode} ({len(product['compOffers'])} found)")
    print(f"  Status    : {product['scrape_status'].upper()}")

    save(product)

    print(f"\n  ✓ Output → {OUTPUT_FILE}")
    print("="*55 + "\n")