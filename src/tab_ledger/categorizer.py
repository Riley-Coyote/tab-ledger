"""Auto-categorization engine for Tab Ledger."""

import re
from urllib.parse import urlparse

# Category definitions: (name, color_hex, patterns)
# Patterns are matched against the full URL or domain
# ORDER MATTERS: first match wins. More specific patterns should come first.
CATEGORIES = [
    ("Google Workspace", "#34A853", [
        r"docs\.google\.com", r"mail\.google\.com", r"drive\.google\.com",
        r"sheets\.google\.com", r"calendar\.google\.com", r"contacts\.google\.com",
    ]),
    ("Google Search", "#4285F4", [
        r"www\.google\.com/search", r"google\.com/\?",
    ]),
    ("X / Twitter", "#E7E9EA", [
        r"x\.com", r"twitter\.com", r"t\.co/",
    ]),
    ("AI Studio", "#A855F7", [
        r"claude\.ai", r"code\.claude\.com", r"claudeusercontent\.com",
        r"chatgpt\.com", r"chat\.openai\.com", r"pay\.openai\.com",
        r"gemini\.google\.com", r"grok\.com", r"kimi\.com",
        r"openrouter\.ai", r"perplexity\.ai", r"poe\.com",
        r"labs\.google", r"aistudio\.google",
        r"anthropic\.skilljar", r"claudecode\.community",
        r"derpseek\.com", r"heylemon\.ai", r"lemonai\.ai",
    ]),
    ("Creative / Gen", "#EC4899", [
        r"midjourney\.com", r"higgsfield\.ai", r"pxlart\.com",
        r"suno\.com", r"udio\.com", r"runway\.ml", r"leonardo\.ai",
    ]),
    ("Design Research", "#F97316", [
        r"dribbble\.com", r"landingfolio\.com", r"pinterest\.com",
        r"fuselabcreative\.com", r"behance\.net", r"figma\.com",
        r"awwwards\.com", r"rubik\.design",
    ]),
    # Add your project-specific URL patterns here, e.g.:
    # ("My Project", "#3B82F6", [
    #     r"myproject\.com", r"localhost:3000",
    # ]),
    ("Crypto", "#EAB308", [
        r"dexscreener\.com", r"margex\.com", r"pump\.fun",
        r"tradingview\.com", r"bankr\.bot", r"birdeye\.so",
        r"raydium\.io", r"jupiter\.ag", r"solscan\.io",
        r"coinmarketcap\.com", r"coingecko\.com",
    ]),
    ("Dev Docs", "#06B6D4", [
        r"threejs\.org", r"observablehq\.com", r"d3-graph-gallery\.com",
        r"mdn\.mozilla\.org", r"developer\.mozilla\.org", r"devdocs\.io",
        r"stackoverflow\.com", r"docs\.python\.org",
    ]),
    ("Infrastructure", "#6B7280", [
        r"platform\.claude\.com", r"platform\.openai\.com",
        r"digitalocean\.com", r"vercel\.com", r"stripe\.com",
        r"godaddy\.com", r"github\.com", r"gitlab\.com",
        r"netlify\.com", r"supabase\.com", r"fly\.io",
        r"cloudflare\.com", r"railway\.app", r"render\.com",
        r"npmjs\.com", r"pypi\.org", r"grafana\.com",
    ]),
    ("Health", "#86EFAC", [
        r"brightside\.com", r"medvidi\.com", r"myhealth",
        r"patient\.portal",
    ]),
    ("Shopping", "#F472B6", [
        r"converse\.com", r"opticabassol\.com", r"americandecaychicago\.com",
        r"eyebuydirect\.com", r"crepprotect\.com", r"fanttik\.com",
        r"nymag\.com", r"travelandleisure\.com", r"warbyparker\.com",
        r"zenni\.com",
    ]),
    ("Gear Research", "#94A3B8", [
        r"amazon\.com", r"bestbuy\.com", r"apple\.com",
        r"etsy\.com", r"ebay\.com", r"newegg\.com",
        r"bhphotovideo\.com", r"rtings\.com", r"us\.nothing\.tech",
    ]),
    ("Deep Research", "#6366F1", [
        r"cia\.gov", r"arxiv\.org", r"consciousness",
        r"scholar\.google", r"researchgate\.net",
        r"nature\.com", r"sciencedirect\.com", r"thoughtinmotion\.net",
    ]),
    ("Social", "#EF4444", [
        r"instagram\.com", r"facebook\.com", r"youtube\.com",
        r"reddit\.com", r"tiktok\.com", r"threads\.net",
    ]),
    ("Local Dev", "#4ADE80", [
        r"localhost:", r"127\.0\.0\.1:", r"0\.0\.0\.0:",
        r"file://",
    ]),
]

# Compile patterns once
_COMPILED = []
for name, color, patterns in CATEGORIES:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    _COMPILED.append((name, color, compiled))

# Stale tab patterns
STALE_PATTERNS = [
    (r"accounts\.google\.com/o/oauth", "Expired OAuth flow"),
    (r"accounts\.google\.com", "Stale Google auth page"),
    (r"localhost.*/callback\?code=", "Completed OAuth callback"),
    (r"checkout\.stripe\.com/", "Completed checkout"),
    (r"/signin/", "Stale login page"),
    (r"/ServiceLogin", "Stale Google login"),
    (r"accounts\.google\.com/.*signin", "Stale Google signin"),
    (r"login\.microsoftonline\.com", "Stale Microsoft login"),
    (r"auth\.opera\.com", "Stale Opera auth"),
    (r"authentication-devices\.checkout\.com", "Stale checkout auth"),
    (r"recaptcha\.net", "Stale reCAPTCHA"),
]

_STALE_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in STALE_PATTERNS]


def categorize_url(url: str) -> tuple[str, str]:
    """Return (category_name, color_hex) for a URL."""
    for name, color, patterns in _COMPILED:
        for pattern in patterns:
            if pattern.search(url):
                return name, color
    return "Uncategorized", "#9CA3AF"


def check_stale(url: str) -> tuple[bool, str | None]:
    """Check if a URL is stale. Returns (is_stale, reason)."""
    for pattern, reason in _STALE_COMPILED:
        if pattern.search(url):
            return True, reason
    return False, None


def get_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc or parsed.path.split("/")[0]
    except Exception:
        return url


def get_category_colors() -> dict[str, str]:
    """Return mapping of category name -> color hex."""
    colors = {name: color for name, color, _ in CATEGORIES}
    colors["Uncategorized"] = "#9CA3AF"
    colors["Claude Code"] = "#D97706"  # Anthropic orange
    return colors


def categorize_cc_session(summary: str, first_prompt: str, project_name: str) -> str:
    """Categorize a Claude Code session by its content."""
    text = f"{summary} {first_prompt} {project_name}".lower()

    # Add your project-specific keywords here to auto-categorize sessions.
    keyword_map = {
        # "My Project": ["my-project", "myapp"],
        "Infrastructure": ["deploy", "vercel", "supabase", "docker", "ci/cd", "github action"],
        "Creative / Gen": ["design", "art", "visual", "creative", "midjourney"],
        "Deep Research": ["research", "arxiv", "analysis"],
        "Crypto": ["token", "solana", "crypto", "dex", "trading"],
        "X / Twitter": ["twitter", "tweet", "x bot", "x api"],
    }

    for category, keywords in keyword_map.items():
        for kw in keywords:
            if kw in text:
                return category

    return "Claude Code"
