import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import streamlit as st

def connect_to_sheet(sheet_name):
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    json_key_path = os.getenv("GOOGLE_KEYFILE")

    if not json_key_path or not os.path.exists(json_key_path):
        st.error("❌ GOOGLE_KEYFILE not set or the file doesn't exist.")
        st.stop()

    creds = ServiceAccountCredentials.from_json_keyfile_name(json_key_path, scope)
    client = gspread.authorize(creds)
    sheet = client.open(sheet_name).sheet1
    return sheet
