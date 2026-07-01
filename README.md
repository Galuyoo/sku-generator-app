# SKU Generator App (Shopify)

A Streamlit-based automation system for generating Shopify-ready product
CSVs and optionally publishing products directly via the Shopify Admin
API.

------------------------------------------------------------------------

## 🚀 Overview

This application automates the product creation workflow for
print-on-demand operations.

It supports:

-   Manual product creation via UI inputs
-   Automated CSV generation from structured Dropbox design folders
-   Live mockup intake for Canva ZIPs plus matching metadata JSON uploads
-   Config-driven garment / size / color mapping
-   Image-to-variant mapping using predefined mockup rules
-   Google Sheets SKU suffix tracking (duplicate protection)
-   Optional direct upload to Shopify via Admin API
-   Batch CSV building with automatic file splitting
-   Dropbox folder lifecycle management (move / archive / cleanup)

------------------------------------------------------------------------

## 🧠 Architecture

### Core Pipeline

1.  Load configuration from `constants/`
2.  Collect metadata (manual input OR Dropbox metadata JSON)
3.  Fetch mockup image links (Dropbox shared links)
4.  Generate Shopify-ready DataFrame (`utils/sku_generator.py`)
5.  Export CSV (with optional size splitting)
6.  Optional: Upload products to Shopify
7.  Optional: Move / clean Dropbox folders

### Separation of Concerns

-   `app.py` --- Streamlit UI + orchestration
-   `utils/sku_generator.py` --- Pure dataframe generation logic
-   `utils/dropbox_utils.py` --- Dropbox integration
-   `utils/google_utils.py` --- Google Sheets SKU tracking
-   `utils/shopify_utils.py` --- Shopify Admin API upload logic
-   `constants/` --- Garment mappings, pricing, sizes, and rules

------------------------------------------------------------------------

## ⚙️ Installation

### 1️⃣ Create Virtual Environment

``` bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2️⃣ Configure Environment Variables

Copy the template file:

``` bash
copy .env.example .env
```

Then fill in your credentials inside `.env`.

------------------------------------------------------------------------

## 🔐 Required Environment Variables

See `.env.example` for full list.

Typical variables:

-   `DROPBOX_REFRESH_TOKEN`
-   `FOLDER_PATH_Design`
-   `SHOPIFY_STORE_URL_TEST`
-   `SHOPIFY_API_PASSWORD_TEST`
-   `SHOPIFY_STORE_URL_PROD`
-   `SHOPIFY_API_PASSWORD_PROD`
-   `GOOGLE_KEYFILE`
-   `SKU_APP_LIVE_DEPLOYMENT=1` to show only the deployable Auto from
    Dropbox workflow. Set it to `0` locally to show the older development
    tabs.

------------------------------------------------------------------------

## ▶️ Run the Application

``` bash
streamlit run app.py
```

------------------------------------------------------------------------

## 📦 Project Structure

    sku-generator-app/
    │
    ├── app.py
    ├── requirements.txt
    ├── .env.example
    │
    ├── constants/
    ├── utils/
    ├── docs/
    └── README.md

------------------------------------------------------------------------

## 🛡️ Security Notes

-   No API keys are stored in the repository.
-   All credentials are loaded via environment variables.
-   `.env` is ignored via `.gitignore`.

------------------------------------------------------------------------

## 📈 Roadmap

-   LLM-based metadata generation
-   Direct Canva mockup link ingestion
-   Full automation pipeline (Dropbox → CSV → Shopify)
-   Headless CLI mode for warehouse automation

------------------------------------------------------------------------

## 🧩 Purpose

This system is designed to: - Reduce manual Shopify product entry -
Enforce SKU consistency - Prevent duplicate suffix usage - Scale
print-on-demand operations efficiently

------------------------------------------------------------------------

Author: Salaheddine Chouikh
