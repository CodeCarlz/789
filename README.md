# PhoneReg — Mobile + OTP Registration CLI

Register an account straight from the terminal: spin a welcome wheel,
enter your mobile number, type the OTP code you receive by SMS,
then spin the reward wheel — all without opening a browser.
---
```
789/
├── registry.py            # the CLI (main script)
├── site_config.json       # site endpoints, field mapping, wheel selectors
├── requirements.txt       # Python dependencies
└── README.md              # this file
```
---
## How It Works

The script fills out the site's own HTML forms like a browser would
(hidden/CSRF fields included), keeps session cookies so the OTP stays
tied to your request, and reports the result.

| Step | What happens |
|------|-------------|
| **1** | Loads the registration URL |
| **2** | Detects the welcome wheel popup → you answer **y** (play) or **n** (skip) |
| **3** | You type a **10-digit mobile number** (sent via the site's form) |
| **4** | You type the **OTP code** from your SMS → submitted automatically |
| **5** | The **reward wheel** spins again after successful registration |


---

## Requirements

- **Python 3.10+**
- `requests`, `beautifulsoup4` (install via `requirements.txt`)
- `playwright` *(optional)* — only for `--browser` mode to click the
  wheel / resend controls in a real browser

---

## Installation

```bash
cd phonereg
pip install -r requirements.txt
:: put the real signup URL into site_config.json -> "register_url"


python register.py --config site_config.json

:: optional: real clicks for wheel + resend button
pip install playwright && playwright install chromium
python register.py --config site_config.json --wheel play --wheel2 play --browser
```