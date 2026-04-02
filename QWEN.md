# Shopee Inventory Calculator (莉莉安蝦皮庫存管理系統)

## Project Overview

A Shopee seller inventory management system that integrates web scraping, sales data analysis, and inventory calculation. The system helps sellers efficiently manage inventory and analyze sales performance.

### Main Components

| File | Purpose |
|------|---------|
| `main.py` | Web server entry point - launches HTTP server and browser UI |
| `crawler.py` | Shopee web scraper using Playwright (Selenium-compatible API) |
| `calculator.py` | PyQt5 GUI inventory calculator (standalone tool) |
| `parser.py` | Excel data parser for Shopee export files |
| `pw_adapter.py` | Playwright ↔ Selenium compatibility layer |
| `build.py` | PyInstaller build script for creating distributable executable |
| `index.html` | Web UI frontend |
| `script.js` | Frontend JavaScript logic |
| `styles.css` | Frontend styling |

### Technologies

- **Backend**: Python 3.6+
- **Web Scraping**: Playwright (Chromium) with custom Selenium compatibility layer
- **GUI**: PyQt5 (for standalone calculator)
- **Web Framework**: Built-in `http.server` module
- **Data Processing**: pandas (Excel parsing)
- **Packaging**: PyInstaller

## Building and Running

### Environment Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### Running the Application

```bash
# Start the main web server (opens browser automatically)
python main.py
```

The server starts at `http://localhost:8080` and provides:
- Shopee product search and scraping
- Sales data analysis
- Inventory calculation dashboard

### Running the Standalone Calculator

```bash
# Launch the PyQt5 inventory calculator GUI
python calculator.py
```

### Running the Data Parser

```bash
# Parse Shopee Excel export files from data/ directory
python parser.py
```

### Building the Executable

```bash
# Create standalone executable (dist/ShopeeCrawler)
python build.py
```

## Project Structure

```
InventoryCalculater/
├── main.py              # Web server + crawler orchestrator
├── crawler.py           # Shopee scraper (Playwright)
├── calculator.py        # PyQt5 inventory calculator
├── parser.py            # Excel data parser
├── pw_adapter.py        # Selenium→Playwright adapter
├── build.py             # PyInstaller build script
├── index.html           # Web UI
├── script.js            # Frontend logic
├── styles.css           # Frontend styles
├── requirements.txt     # Python dependencies
├── golden_table.json    # Reference data table
├── cookies.json         # Shopee login cookies (user-provided)
├── data/                # Excel export files (git-ignored)
│   ├── 主庫存/          # Main stock files
│   └── parentskudetail*.xlsx  # Monthly sales files
└── dist/                # Built executables
```

## Key Features

### Web Scraper (`crawler.py`)
- Logs into Shopee Seller Center using cookies
- Scrapes product listings with variants/models
- Extracts monthly sales data from Shopee Data Center
- Handles popups and anti-bot measures
- Supports headless and visible browser modes

### Inventory Calculator (`calculator.py`)
- PyQt5 desktop GUI with dark theme
- Calculates expected inventory based on:
  - Product sold ratio
  - Total sales volume
  - Monthly sales average
  - Current inventory
  - Target months coverage
- Color-coded inventory status (red/yellow/green)
- Special handling for zero inventory scenarios

### Data Parser (`parser.py`)
- Parses Shopee Excel export files
- Merges stock, sales, and media data
- Outputs consolidated `shopee_products.json`

### Playwright Adapter (`pw_adapter.py`)
- Provides Selenium-compatible API for Playwright
- Supports `By`, `WebDriverWait`, `ActionChains`, etc.
- Enables gradual migration from Selenium

## Configuration

### Cookies Setup
The scraper requires Shopee cookies for authentication:
1. Install a cookie editor extension (e.g., Cookie-Editor)
2. Log into Shopee Seller Center
3. Export cookies as JSON
4. Save as `cookies.json` in project root

### Chrome/Chromium
- Playwright auto-manages ChromeDriver via `webdriver-manager`
- Manual ChromeDriver installation is optional

## Development Notes

### Coding Conventions
- Traditional Python style with docstrings
- Mixed English/Chinese comments and identifiers
- Extensive error handling with logging

### Testing Practices
- No formal test suite present
- Manual testing via web UI and CLI

### Known Patterns
- Heavy use of `try/except` for robustness
- JavaScript injection for anti-detection
- Multi-threading for real-time output streaming
- Process management with `psutil` for cleanup

## Dependencies

```
playwright       # Web automation
psutil           # Process management
PyQt5            # GUI framework
requests         # HTTP client
pyinstaller      # Executable packaging
pandas           # Excel parsing (used by parser.py)
```

## Common Commands

```bash
# Search products via web UI
# Open http://localhost:8080 and use the search interface

# Stop running crawler
# Click "Stop" button in web UI or send POST /stop_crawler

# Parse new Excel data
# Place files in data/ directory and run: python parser.py
```
