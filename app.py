import streamlit as st
import google.generativeai as genai
import json
import datetime
import urllib.parse
import pandas as pd
import gspread
import re
import time
from google.api_core.exceptions import DeadlineExceeded, ResourceExhausted


# --- PAGE CONFIGURATION & STYLING ---
st.set_page_config(page_title="Home Meal Planner", layout="wide")

# Custom CSS for enhanced dropdown height, row expansion, and clean text wrapping
st.markdown("""
    <style>
    div[data-baseweb="select"] > div {
        height: auto !important;
        min-height: 45px !important;
        white-space: normal !important;
        padding-top: 4px !important;
        padding-bottom: 4px !important;
    }
    div[data-baseweb="popover"] div[role="option"],
    div[data-baseweb="menu"] div[role="option"],
    ul[role="listbox"] li {
        white-space: normal !important;
        word-wrap: break-word !important;
        overflow-wrap: break-word !important;
        height: auto !important;
        line-height: 1.5 !important;
        padding-top: 10px !important;
        padding-bottom: 10px !important;
    }
    .stMarkdown, p, span, div, label, h1, h2, h3, h4, h5, h6 {
        word-wrap: break-word !important;
        overflow-wrap: break-word !important;
        white-space: normal !important;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🌾 Smart Meal Planner")

# --- SECRETS & API CONFIGURATION ---
try:
    GENAI_API_KEY = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=GENAI_API_KEY)
    model = genai.GenerativeModel('gemini-3.8-flash')
except Exception as e:
    st.error("⚠️ Please configure your GEMINI_API_KEY in Streamlit secrets (.streamlit/secrets.toml).")

# --- ROBUST AUTO-RETRY WRAPPER (Handles 429 Rate Limits & 504 Timeouts) ---
def call_gemini_with_retry(prompt, generation_config=None, max_retries=3):
    """Calls Gemini model with automatic retry on 429 rate limit and 504 timeout errors."""
    for attempt in range(max_retries):
        try:
            if generation_config:
                return model.generate_content(prompt, generation_config=generation_config)
            else:
                return model.generate_content(prompt)
        except (DeadlineExceeded, Exception) as ex:
            error_str = str(ex)
            is_rate_limit = "429" in error_str or "Quota exceeded" in error_str or isinstance(ex, ResourceExhausted)
            is_timeout = "Deadline exceeded" in error_str or "504" in error_str or isinstance(ex, DeadlineExceeded)
            
            if is_rate_limit or is_timeout:
                if attempt < max_retries - 1:
                    wait_time = 15 * (attempt + 1)
                    reason = "Rate limit (429)" if is_rate_limit else "Timeout (504)"
                    st.warning(f"⚠️ {reason} encountered. Retrying in {wait_time}s (Attempt {attempt+1}/{max_retries})...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception("Operation failed repeatedly due to network limits or timeouts. Please try again in a moment.")
            else:
                raise ex

# --- INITIALIZE SESSION STATE ---
if "pending_log" not in st.session_state:
    st.session_state.pending_log = None
if "history_db" not in st.session_state:
    st.session_state.history_db = []
if "modular_options" not in st.session_state:
    st.session_state.modular_options = None
if "grocery_list" not in st.session_state:
    st.session_state.grocery_list = None
if "static_batch_tip" not in st.session_state:
    st.session_state.static_batch_tip = None
if "fruit_text_input" not in st.session_state:
    st.session_state.fruit_text_input = "Banana"

# --- HELPERS ---
def get_fruit_emoji(fruit_name):
    fn = str(fruit_name).lower().strip()
    if "apple" in fn: return "🍎"
    elif "banana" in fn: return "🍌"
    elif "mango" in fn: return "🥭"
    elif "orange" in fn or "citrus" in fn or "mosambi" in fn: return "🍊"
    elif "grape" in fn: return "🍇"
    elif "watermelon" in fn: return "🍉"
    elif "papaya" in fn: return "🍈"
    elif "guava" in fn or "pear" in fn: return "🍐"
    elif "pomegranate" in fn or "berry" in fn: return "🍓"
    elif "pineapple" in fn: return "🍍"
    else: return "🍎"

def categorize_plant_ingredient(plant_name):
    pn = plant_name.lower().strip()
    greens_keywords = ['keerai', 'spinach', 'methi', 'muringai', 'coriander', 'mint', 'pudina', 'kariveppilai', 'curry leaf', 'lettuce', 'kale', 'palak', 'amaranth', 'gongura']
    roots_keywords = ['carrot', 'potato', 'beetroot', 'radish', 'mullangi', 'yam', 'senai', 'sweet potato', 'onion', 'garlic', 'ginger', 'turmeric', 'colocasia', 'arbi']
    millets_keywords = ['millet', 'thinai', 'samai', 'ragi', 'kuthiraivali', 'varagu', 'kambu', 'jowar', 'bajra', 'oats', 'quinoa', 'brown rice', 'red rice', 'wheat']
    lentils_keywords = ['dal', 'dhal', 'sambar', 'kootu', 'sundal', 'chana', 'rajma', 'peanuts', 'chickpea', 'moong', 'urad', 'toor', 'gram', 'soybean', 'sprouts', 'lentil']
    
    if any(k in pn for k in greens_keywords): return "Leafy Greens (Keerai)"
    elif any(k in pn for k in roots_keywords): return "Roots & Tubers"
    elif any(k in pn for k in millets_keywords): return "Millets & Grains"
    elif any(k in pn for k in lentils_keywords): return "Lentils & Legumes"
    else: return "Vegetables & Fruits"

# --- GOOGLE SHEETS HELPERS ---
SHEET_URL = st.secrets["SHEET_URL"]

def get_gspread_worksheet(worksheet_name):
    creds_dict = dict(st.secrets["gcp_service_account"])
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_url(SHEET_URL)
    return sh.worksheet(worksheet_name)

@st.cache_data(ttl=30)
def fetch_pantry_full_records():
    try:
        ws = get_gspread_worksheet("Pantry_Inventory")
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        df.columns = df.columns.str.strip()
        return df.to_dict(orient='records')
    except Exception as ex:
        return []

@st.cache_data(ttl=30)
def fetch_vegetables_and_categories():
    try:
        records = fetch_pantry_full_records()
        veggies, low_veggies, millets = [], [], []
        categories = set()
        for row in records:
            cat = str(row.get('Category', '')).strip()
            if cat: categories.add(cat)
            status = str(row.get('Status', 'In Stock')).strip().lower()
            item = str(row.get('Item name', '')).strip()
            if status not in ['out of stock'] and item and item.lower() != 'nan':
                if 'veg' in cat.lower():
                    display_label = f"{item} (Low Stock)" if status == 'low' else item
                    veggies.append(display_label)
                    if status == 'low': low_veggies.append(item)
                elif any(m in item.lower() or m in cat.lower() for m in ['millet', 'thinai', 'samai', 'ragi', 'kuthiraivali', 'varagu']):
                    millets.append(item)
        return veggies, low_veggies, millets, sorted(list(categories)) if categories else ["Vegetables", "Staples", "Fruits", "Millets", "Grocery"]
    except Exception as ex:
        return [], [], [], ["Vegetables", "Staples", "Fruits", "Millets", "Grocery"]

@st.cache_data(ttl=30)
def fetch_available_fruits():
    try:
        records = fetch_pantry_full_records()
        fruits = []
        for row in records:
            cat = str(row.get('Category', '')).strip().lower()
            status = str(row.get('Status', 'In Stock')).strip().lower()
            item = str(row.get('Item name', '')).strip()
            if 'fruit' in cat and status != 'out of stock' and item and item.lower() != 'nan':
                fruits.append(item)
        return fruits
    except Exception as ex:
        return []

@st.cache_data(ttl=30)
def fetch_family_favorites():
    try:
        ws = get_gspread_worksheet("Family_Favorites")
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        df.columns = df.columns.str.strip()
        return df.to_dict(orient='records')
    except Exception as ex:
        return []

def update_existing_pantry_item(item_name, new_status):
    ws = get_gspread_worksheet("Pantry_Inventory")
    records = ws.get_all_records()
    target_row = None
    clean_target = item_name.strip().lower()
    for idx, row in enumerate(records, start=2):
        normalized_row = {str(k).strip().lower(): str(v).strip() for k, v in row.items()}
        if normalized_row.get('item name', '').lower() == clean_target:
            target_row = idx
            break
    if target_row:
        ws.update_cell(target_row, 3, new_status)
        st.cache_data.clear()
        return True, f"Successfully updated '{item_name}' status to '{new_status}'!"
    else:
        return False, f"Could not find '{item_name}' in Google Sheet."

def append_new_pantry_item(category, item_name, status):
    ws = get_gspread_worksheet("Pantry_Inventory")
    records = ws.get_all_records()
    clean_target = item_name.strip().lower()
    for row in records:
        normalized_row = {str(k).strip().lower(): str(v).strip() for k, v in row.items()}
        if normalized_row.get('item name', '').lower() == clean_target:
            return update_existing_pantry_item(item_name, status)
    ws.append_row([category, item_name, status])
    st.cache_data.clear()
    return True, f"Successfully added new item '{item_name}' as '{status}' under '{category}'!"

def save_meal_history_to_sheet(p):
    ws = get_gspread_worksheet("Meal_History")
    ws.append_row([
        p.get("date"), p.get("tiffin_morning"), p.get("tiffin_morning_side"),
        p.get("lunch_main"), p.get("lunch_side"), p.get("tiffin_dinner"),
        p.get("tiffin_dinner_side"), p.get("fruit"), ", ".join(p.get("veggies_used", []))
    ])

def update_multiple_item_statuses(status_updates):
    if not status_updates: return
    ws = get_gspread_worksheet("Pantry_Inventory")
    records = ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        normalized_row = {str(k).strip().lower(): str(v).strip() for k, v in row.items()}
        sheet_item = normalized_row.get('item name', '')
        for item_key, new_status in status_updates.items():
            if sheet_item.lower() == item_key.lower():
                ws.update_cell(idx, 3, new_status)
    st.cache_data.clear()

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ App Mode")
    GUEST_MODE_LABEL = "👤 Quick Guest Mode"
    SELF_MODE_LABEL = "🔒 Admin / Self Mode (PIN Protected)"
    mode_selection = st.radio("Select Usage Mode", [GUEST_MODE_LABEL, SELF_MODE_LABEL])
    
    quick_guest_mode = True
    if mode_selection == SELF_MODE_LABEL:
        admin_pin = st.text_input("Enter Admin PIN:", type="password")
        if admin_pin != st.secrets["ADMIN_PIN"]:
            if admin_pin != "": st.error("Incorrect PIN! Defaulting to Guest Mode.")
            quick_guest_mode = True
        else:
            quick_guest_mode = False
            st.success("Admin / Self Mode Unlocked!")
    else:
        quick_guest_mode = True

    st.markdown("---")
    if not quick_guest_mode:
        st.header("📦 Pantry Stock Management")
        all_records = fetch_pantry_full_records()
        veggies_list, low_stock_items, millets_list, dynamic_categories = fetch_vegetables_and_categories()
        
        pantry_action = st.radio("Action Type:", ["🔄 Update Existing Stock", "➕ Add New Item"], key="pantry_action_radio")
        status_options = ["In Stock", "Low", "Out of Stock"]
        
        if pantry_action == "🔄 Update Existing Stock":
            existing_items = sorted(list(set([str(r.get('Item name', '')).strip() for r in all_records if r.get('Item name')])))
            if existing_items:
                selected_item = st.selectbox("Search & Select Item:", existing_items)
                matched_row = next((r for r in all_records if str(r.get('Item name', '')).strip() == selected_item), None)
                current_status = matched_row.get('Status', 'In Stock') if matched_row else 'In Stock'
                new_status = st.selectbox("New Stock Status:", status_options, index=status_options.index(current_status) if current_status in status_options else 0)
                if st.button("💾 Update Stock"):
                    success, msg = update_existing_pantry_item(selected_item, new_status)
                    if success: st.success(msg)
                    else: st.error(msg)
            else: st.info("No items found.")
        else:
            with st.form("add_new_pantry_item_form", clear_on_submit=True):
                new_category = st.selectbox("Category:", dynamic_categories)
                new_item_name = st.text_input("New Item Name:")
                initial_status = st.selectbox("Initial Stock Status:", status_options)
                if st.form_submit_button("➕ Add Item to Sheet"):
                    if new_item_name.strip():
                        success, msg = append_new_pantry_item(new_category, new_item_name.strip(), initial_status)
                        if success: st.success(msg)
                        else: st.error(msg)
                    else: st.error("Please enter a valid item name.")
    else:
        st.info("👤 **Guest Mode Active**")

# --- TABS ---
tab1, tab2, tab3 = st.tabs(["📅 Daily Menu Planner", "📦 Grocery & Stocking", "📊 Monthly Diversity Analytics"])

with tab1:
    st.subheader("📅 Modular Daily Menu Builder")
    guest_veggies_input = ""
    if quick_guest_mode:
        st.info("💡 **Quick Guest Input:**")
        guest_veggies_input = st.text_input("Vegetables & Staples Today:", placeholder="e.g., Carrot, Potato, Thinai")
    
    with st.container():
        st.markdown("### ⚙️ Daily Kitchen Readiness")
        col_c1, col_c2, col_c3 = st.columns(3)
        with col_c1: batter_available = st.checkbox("Is Idli / Dosa Batter available?", value=False)
        with col_c2: use_favorites = st.checkbox("Include Family Favorites?", value=True)
        with col_c3: leftovers_input = st.text_input("Any Leftovers to Finish?", placeholder="e.g., Sambar")

    if st.button("✨ Generate Modular Menu"):
        with st.spinner("Quickly crafting your meal options..."):
            if quick_guest_mode:
                flat_veggies = guest_veggies_input if guest_veggies_input.strip() else "Standard mixed vegetables"
                low_str, millets_str, fruits_str, favorites = "None", "Thinai, Samai", "Banana, Apple, Papaya", []
            else:
                veggies_list, low_stock_items, millets_list, _ = fetch_vegetables_and_categories()
                available_fruits = fetch_available_fruits()
                flat_veggies = ", ".join(veggies_list) if veggies_list else "Standard veggies"
                low_str = ", ".join(low_stock_items) if low_stock_items else "None"
                millets_str = ", ".join(millets_list) if millets_list else "None"
                fruits_str = ", ".join(available_fruits) if available_fruits else "Seasonal Fruit"
                favorites = fetch_family_favorites() if use_favorites else []
            
            fav_str = json.dumps(favorites[:10]) if favorites else "None"
            fav_rule = f"6. Incorporate Family Favorites: {fav_str}." if (use_favorites and not quick_guest_mode) else "6. Actively suggest diverse dishes."
            batter_status_text = 'AVAILABLE' if batter_available else 'NOT AVAILABLE (No Idli/Dosa)'

            # Updated prompt 
            prompt = f"""
            You are an expert South Indian culinary specialist. Generate a cohesive daily menu in strict JSON format.

            MANDATORY FORMATTING RULE:
            Every dish name field must combine the main item and side item using ONLY a plus sign with spaces (" + "). 
            CORRECT EXAMPLE: "Chapathi + Drumstick Sambar"
            INCORRECT EXAMPLE: "Chapathi with Drumstick Sambar" or "Chapathi and Sambar"

            Return a JSON object with these exact keys:
            - "breakfast_options": Array of 3 objects, each with "option_id", "name" (e.g., "Rava Upma + Coconut Chutney"), and "veggies_used" (array of strings).
            - "lunch_options": Array of 3 objects, each with "option_id", "name" (e.g., "Steamed Rice + Drumstick Shallot Sambar + Cabbage Poriyal"), and "veggies_used".
            - "dinner_options": Array of 3 objects, each with "option_id", "name", and "veggies_used". 
              DINNER RULE: Dinner main must be a tiffin item, and the side MUST use the exact same side dish name from Lunch or Breakfast, joined by " + ".
            - "fruit": String (Pick ONE from [{fruits_str}]).

            CONTEXT & CONSTRAINTS:
            1. Pure vegetarian. 
            2. Batter status: {batter_status_text}. 
            3. Leftovers to finish: {leftovers_input or 'None'}. 
            4. Available veggies: {flat_veggies}. 
            5. LOW STOCK items [{low_str}]: Use ONLY for Sambar/Kootu. 
            6. MILLETS [{millets_str}]: Suggest occasionally. {fav_rule}
            """
            try:
                response = call_gemini_with_retry(prompt, generation_config={"response_mime_type": "application/json"})
                st.session_state.modular_options = json.loads(response.text)
                st.session_state.fruit_text_input = st.session_state.modular_options.get('fruit', 'Banana')
                st.session_state.static_batch_tip = None
            except Exception as ex:
                st.error(f"Error generating menu: {ex}")

    if st.session_state.modular_options:
        mod = st.session_state.modular_options
        st.markdown("---")
        current_fruit_val = st.session_state.get('fruit_text_input', mod.get('fruit', 'Banana'))
        st.markdown(f"### {get_fruit_emoji(current_fruit_val)} Fruit of the Day")
        final_fruit_choice = st.text_input("Gemini picked this fruit:", key="fruit_text_input")
        
        col_b, col_l, col_d = st.columns(3)
        with col_b:
            st.markdown("### 🌅 Breakfast")
            b_choices = {opt.get('name'): opt for opt in mod.get('breakfast_options', [])}
            b_choices["[Custom / Out of Box]"] = "custom"
            sel_b_name = st.selectbox("Select Breakfast", list(b_choices.keys()), key="sel_b")
            selected_b = {"name": f"{st.text_input('Custom B Main', 'Upma')} + {st.text_input('Custom B Side', 'Chutney')}", "veggies_used": []} if sel_b_name == "[Custom / Out of Box]" else b_choices[sel_b_name]

        with col_l:
            st.markdown("### 🍛 Lunch")
            l_choices = {opt.get('name'): opt for opt in mod.get('lunch_options', [])}
            l_choices["[Custom / Out of Box]"] = "custom"
            sel_l_name = st.selectbox("Select Lunch", list(l_choices.keys()), key="sel_l")
            selected_l = {"name": f"{st.text_input('Custom L Main', 'Rice')} + {st.text_input('Custom L Side', 'Sambar')}", "veggies_used": []} if sel_l_name == "[Custom / Out of Box]" else l_choices[sel_l_name]

        with col_d:
            st.markdown("### 🌙 Dinner")
            d_choices = {opt.get('name'): opt for opt in mod.get('dinner_options', [])}
            d_choices["[Custom / Out of Box]"] = "custom"
            sel_d_name = st.selectbox("Select Dinner", list(d_choices.keys()), key="sel_d")
            selected_d = {"name": f"{st.text_input('Custom D Main', 'Chapathi')} + {st.text_input('Custom D Side', 'Curry')}", "veggies_used": []} if sel_d_name == "[Custom / Out of Box]" else d_choices[sel_d_name]

        if st.button("📋 Prepare Final Daily Plan for Confirmation"):
            def parse_parts(ds):
                ds = str(ds).strip()
                if "+" in ds: parts = ds.split("+", 1)
                elif " with " in ds.lower(): parts = re.split(r'\swith\s', ds, flags=re.IGNORECASE, maxsplit=1)
                else: parts = [ds, ""]
                return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""
            b_m, b_s = parse_parts(selected_b.get('name', ''))
            l_m, l_s = parse_parts(selected_l.get('name', ''))
            d_m, d_s = parse_parts(selected_d.get('name', ''))
            st.session_state.pending_log = {
                "date": str(datetime.date.today()), "tiffin_morning": b_m, "tiffin_morning_side": b_s,
                "lunch_main": l_m, "lunch_side": l_s, "tiffin_dinner": d_m, "tiffin_dinner_side": d_s,
                "fruit": final_fruit_choice.strip() or "Seasonal Fruit", "veggies_used": list(set(selected_b.get('veggies_used', []) + selected_l.get('veggies_used', []) + selected_d.get('veggies_used', [])))
            }
            st.success("Plan prepared!")

    if st.session_state.pending_log:
        st.markdown("---")
        st.subheader("📋 Confirmation & Stock Adjustment")
        p = st.session_state.pending_log
        st.json(p)
        depletion_updates = {}
        if not quick_guest_mode and p.get("veggies_used"):
            for veg in p.get("veggies_used"):
                depletion_updates[veg] = st.selectbox(f"Status for '{veg}':", ["In Stock", "Low", "Out of Stock"], index=2, key=f"dep_{veg}")
        col_s, col_c = st.columns(2)
        with col_s:
            if st.button("✅ Confirm & Save"):
                st.session_state.history_db.append(p)
                if not quick_guest_mode:
                    save_meal_history_to_sheet(p)
                    if depletion_updates: update_multiple_item_statuses(depletion_updates)
                st.success("Saved successfully!")
                
                # Zero-cost deterministic static tip check (No API call)
                d_side = p.get('tiffin_dinner_side', '').strip().lower()
                l_side = p.get('lunch_side', '').strip().lower()
                b_side = p.get('tiffin_morning_side', '').strip().lower()
                
                if d_side and (d_side in l_side or l_side in d_side):
                    st.session_state.static_batch_tip = f"💡 **Batch Tip:** Scale up your **{p.get('lunch_side')}** during Lunch prep so you automatically have enough portioned for Dinner!"
                elif d_side and (d_side in b_side or b_side in d_side):
                    st.session_state.static_batch_tip = f"💡 **Batch Tip:** Prepare extra **{p.get('tiffin_morning_side')}** during Morning Tiffin to carry over for Dinner!"
                else:
                    st.session_state.static_batch_tip = "💡 **Kitchen Note:** Plan your vegetable chopping in the morning to cover both lunch and dinner sides."
                
                st.session_state.pending_log = None
        with col_c:
            if st.button("❌ Discard"): st.session_state.pending_log = None

    if st.session_state.static_batch_tip:
        st.markdown("---")
        st.info(st.session_state.static_batch_tip)

with tab2:
    st.subheader("🛒📦 Grocery Advisor")
    if quick_guest_mode: st.info("Guest Mode active.")
    else:
        if st.button("Analyze Pantry & Get Shopping List"):
            with st.spinner("Analyzing inventory..."):
                records = fetch_pantry_full_records()
                critical_records = [r for r in records if str(r.get('Status', '')).strip().lower() in ['low', 'out of stock']]
                summary = ", ".join([f"{r.get('Item name')} [{r.get('Status')}]" for r in critical_records if r.get('Item name')])
                if not summary: summary = "All main items currently in stock. Suggest routine grocery top-ups."
                res = call_gemini_with_retry(f"Generate concise shopping list for items: [{summary}], focusing on low/out-of-stock items and 30-plant diversity.")
                st.session_state.grocery_list = res.text
        if st.session_state.grocery_list: st.markdown(st.session_state.grocery_list)

with tab3:
    st.subheader("📊 🌱 Monthly Diversity Analytics")
    df_history = pd.DataFrame()
    try:
        ws_hist = get_gspread_worksheet("Meal_History")
        hist_records = ws_hist.get_all_records()
        if hist_records: df_history = pd.DataFrame(hist_records)
    except Exception:
        if len(st.session_state.history_db) > 0: df_history = pd.DataFrame(st.session_state.history_db)

    if not df_history.empty:
        df_history.columns = df_history.columns.str.strip()
        all_plants = set()
        for _, row in df_history.iterrows():
            v_str = str(row.get('veggies_used', ''))
            if v_str and v_str.lower() != 'nan':
                for v in v_str.split(','):
                    if v.strip(): all_plants.add(v.strip().title())
            f_str = str(row.get('fruit', ''))
            if f_str and f_str.lower() != 'nan': all_plants.add(f_str.strip().title())
        
        unique_count = len(all_plants)
        st.metric(label="🌱 Unique Plants Consumed", value=f"{unique_count} / 30")
        st.progress(min(unique_count / 30, 1.0))
        
        categories_map = {"Leafy Greens (Keerai)": [], "Roots & Tubers": [], "Millets & Grains": [], "Lentils & Legumes": [], "Vegetables & Fruits": []}
        for p in sorted(list(all_plants)):
            categories_map[categorize_plant_ingredient(p)].append(p)
        
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(f"**🥬 Greens:** {', '.join(categories_map['Leafy Greens (Keerai)']) or 'None'}")
            st.markdown(f"**🥔 Roots:** {', '.join(categories_map['Roots & Tubers']) or 'None'}")
        with c2:
            st.markdown(f"**🌾 Millets:** {', '.join(categories_map['Millets & Grains']) or 'None'}")
            st.markdown(f"**🫘 Lentils:** {', '.join(categories_map['Lentils & Legumes']) or 'None'}")
        with c3:
            st.markdown(f"**🍎 Others:** {', '.join(categories_map['Vegetables & Fruits']) or 'None'}")
        st.dataframe(df_history)
    else: st.info("No logs yet.")
