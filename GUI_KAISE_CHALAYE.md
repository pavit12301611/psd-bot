# psd.ai GUI kaise chalaye? (Desktop App Guide)

Ab psd.ai koi website **nahi** hai — ye ek asli **desktop application** hai
(Qt/PySide6 se bani hui). Koi browser, koi `localhost:7000` page, aur koi
doosra console window nahi. Sab kuch ek hi window me chalta hai.

## Pehli baar setup (Windows)

1. Repo folder me **`run.bat`** par double-click karo.
2. Pehli baar ye sab hoga (apne aap, ek hi baar):
   - Python 3.11+ dhundhega (nai hai to <https://www.python.org/downloads/> se
     install karo, **"Add python.exe to PATH"** tick karna mat bhoolna)
   - `venv` banayega aur saari dependencies install karega (Qt/PySide6 samet —
     internet speed ke hisaab se kuch minute lag sakte hain)
   - `setup.py` chalayega (data folders, database, `.env`)
3. Uske baad **psd.ai ki desktop window** khul jayegi.
4. Pehli start par window me **"Create your admin account"** screen aayegi:
   - Username chooso (jaise `admin`)
   - Password chooso (kam se kam 8 character)
   - **Create account** dabao — bas, andar aa jaoge.
5. Agli baar se `run.bat` double-click karte hi window seedha khul jayegi
   (usi machine par sign-in yaad rehta hai).

> Bar bar kuch install nahi hota — jo step ho chuka hai wo skip ho jata hai.

## Window ka naksha

| Left sidebar | Kaam |
| --- | --- |
| **Chat** | Model se baat — jawab stream hoke aata hai; agent tools, approvals, files attach, sessions left panel me |
| **Documents** | Docs banao/padho/edit karo, markdown rendering ke saath |
| **Notes / Tasks / Calendar / Email** | Poora workspace — notes, scheduled tasks, events, email bhejna/padhna |
| **Gallery** | Images — upload, preview, rename/rotate, AI tags |
| **Research** | Deep research chalao, report library me padho |
| **Models** | API endpoints jodo (OpenAI-style, Groq, OpenRouter, local...) aur default chat model chuno |
| **Local Models** | Apne PC par GGUF model download + llama.cpp server group — progress bar aur start/stop **window ke andar** |
| **Memory / Skills / MCP Servers** | Agent ki yaaddasht, seekhe hue tareeke, aur MCP tools |
| **Settings** | Theme (16 themes!), font, account + 2FA, assistant providers, data export/import |
| **Diagnostics** | Services ki health + live log — console window ki zaroorat nahi |

## Local models (bina doosre console window ke)

Pehle ye kaam ek alag cmd window me hota tha — ab **sab app ke andar** hai:

1. Sidebar me **Local Models** kholo.
2. Upar **"This machine"** card tumhara RAM/CPU/GPU dikhata hai, aur neeche
   **hardware-fit table** batati hai kaunse models tumhare PC par chal sakte
   hain (fit level + score ke saath).
3. Model select karo → **Download selected** — download progress window me
   dikhta rehta hai (pehli baar model size ke hisaab se time lagega).
4. **Start model group** dabao — har model ka ek server chal padega (port 8080
   se aage), unka output neeche log pane me aata hai, aur status pill
   "running" ho jayega. Ye servers **Models** me endpoints ki tarah register
   ho jate hain, aur default chat model bhi ban sakta hai.
5. Kaam khatam? **Stop** dabao — saare servers band. Window band karne par bhi
   sab kuch saaf band ho jata hai (koi orphan process nahi).

Cache `psd.ai/runtime/` folder me rehta hai, isliye agli baar start me seconds
lagte hain.

## Rozmarra ke shortcuts

- `Ctrl+T` — nayi chat
- `Ctrl+Q` ya menu **File → Quit** — app band
- Window ka **X** — app tray me chali jati hai (tray icon par click karke wapas
  kholo; tray menu se **Quit** bhi kar sakte ho)
- `Enter` — message bhejo, `Shift+Enter` — nayi line
- Theme badalni hai? **Settings → Appearance** me theme tile par click karo —
  poori window turant badal jati hai aur pasand yaad rehti hai.

## Agar kuch gadbad ho

- **Window khuli hi nahi:** `run.bat` wala console dekho — error likha hoga
  (zyada tar Python ya pip install ka issue). Debug ke liye
  `set PSD_GUI_CONSOLE=1` karke `run.bat` chalao, taaki app usi console me
  chale aur log dikhe.
- **Login bhool gaye:** data folder `psd.ai/data/auth.json` me users hain;
  naya setup chahiye to wo file hata do (dhyan rahe: sirf auth reset hoga).
- **Model jawab nahi de raha:** **Diagnostics** me live log dekho, ya
  **Models** me endpoint ka status/probe check karo.
- **PySide6 missing error:** `psd.ai/venv` folder delete karke `run.bat`
  dobara chalao — sab kuch fresh install ho jayega.

## Linux / macOS

```bash
cd psd.ai
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
PSD_AI_SKIP_ADMIN_CREATION=1 python setup.py
python psd_gui.py          # desktop window
```

Pehli baar window me admin account ban jayega. (Linux par Qt ke liye system
libraries chahiye ho sakti hain: `libGL`, `libxkbcommon`, `libdbus`.)

## Purana web mode chahiye?

Desktop app ab default hai. Legacy browser front end abhi bhi repo me hai
(`psd.ai/website/`) — manual instructions ke liye
[`psd.ai/README.md`](psd.ai/README.md) dekho. Roz ke use ke liye desktop window
hi sahi rahegi: tez, private, aur bina kisi port ke.
