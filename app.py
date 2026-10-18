import streamlit as st
import google.generativeai as genai
import json
import datetime
import urllib.parse
import pandas as pd
import gspread
import re


# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Home Meal Planner", layout="wide")

st.title("🌾 Smart Meal Planner")

# --- SECRETS & API CONFIGURATION ---
try:
    GENAI_API_KEY = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=GENAI_API_KEY)
    model = genai.GenerativeModel('gemini-3.8-flash')
except Exception as e:
    st.error("⚠️ Please configure your GEMINI_API_KEY in Streamlit secrets (.streamlit/secrets.toml).")

# --- INITIALIZE SESSION STATE ---
if "pending_log" not in st.session_state:
    st.session_state.pending_log = None
if "history_db" not in st.session_state:
    st.session_state.history_db = []
if "modular_options" not in st.session_state:
    st.session_state.modular_options = None
if "grocery_list" not in st.session_state:
    st.session_state.grocery_list = None
if "batch_plan" not in st.session_state:
    st.session_state.batch_plan = None

# --- GOOGLE SHEETS CONNECTION HELPERS ---
# Loaded securely from Streamlit secrets
SHEET_URL = st.secrets["SHEET_URL"]

def get_gspread_worksheet(worksheet_name):
    """Returns worksheet instance using Streamlit secrets."""
    if "gcp_service_account" not in st.secrets:
        raise Exception("Missing '[gcp_service_account]' block in .streamlit/secrets.toml file.")
    creds_dict = dict(st.secrets["gcp_service_account"])
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_url(SHEET_URL)
    return sh.worksheet(worksheet_name)

@st.cache_data(ttl=30)
def fetch_pantry_full_records():
    """Fetches all raw records from Pantry_Inventory tab."""
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
    """Fetches available vegetables for display and dynamic categories from sheet."""
    try:
        records = fetch_pantry_full_records()
        veggies = []
        low_veggies = []
        categories = set()
        
        for row in records:
            cat = str(row.get('Category', '')).strip()
            if cat:
                categories.add(cat)
                
            status = str(row.get('Status', 'In Stock')).strip().lower()
            item = str(row.get('Item name', '')).strip()
            
            if 'veg' in cat.lower() and status not in ['out of stock'] and item and item.lower() != 'nan':
                display_label = f"{item} (Low Stock)" if status == 'low' else item
                veggies.append(display_label)
                if status == 'low':
                    low_veggies.append(item)
                    
        sorted_categories = sorted(list(categories)) if categories else ["Vegetables", "Staples", "Fruits"]
        return veggies, low_veggies, sorted_categories
    except Exception as ex:
        return [], [], ["Vegetables", "Staples", "Fruits"]

@st.cache_data(ttl=30)
def fetch_available_fruits():
    """Fetches fruits from Pantry_Inventory that are In Stock or Low."""
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
    """Fetches family favorite dishes from Family_Favorites tab."""
    try:
        ws = get_gspread_worksheet("Family_Favorites")
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        df.columns = df.columns.str.strip()
        return df.to_dict(orient='records')
    except Exception as ex:
        return []

def upsert_pantry_item(category, item_name, status):
    """Robust index-based upsert to prevent duplicates."""
    ws = get_gspread_worksheet("Pantry_Inventory")
    records = ws.get_all_records()
    item_found = False
    target_row = None
    
    clean_target = item_name.strip().lower()
    for idx, row in enumerate(records, start=2):
        sheet_item = str(row.get('Item name', '')).strip().lower()
        if sheet_item == clean_target:
            target_row = idx
            item_found = True
            break
            
    if item_found:
        ws.update_cell(target_row, 1, category)
        ws.update_cell(target_row, 3, status)
        msg = f"Successfully updated '{item_name}' status to {status}!"
    else:
        ws.append_row([category, item_name, status])
        msg = f"Successfully added '{item_name}' as {status}!"
        
    st.cache_data.clear()
    return True, msg

def save_meal_history_to_sheet(p):
    """Appends confirmed meal to Meal_History sheet tab."""
    ws = get_gspread_worksheet("Meal_History")
    ws.append_row([
        p.get("date"),
        p.get("tiffin_morning"),
        p.get("tiffin_morning_side"),
        p.get("lunch_main"),
        p.get("lunch_side"),
        p.get("tiffin_dinner"),
        p.get("tiffin_dinner_side"),
        p.get("fruit"),
        ", ".join(p.get("veggies_used", []))
    ])

def update_multiple_item_statuses(status_updates):
    """Updates specific item statuses based on interactive user selection."""
    if not status_updates:
        return
    ws = get_gspread_worksheet("Pantry_Inventory")
    records = ws.get_all_records()
    
    for idx, row in enumerate(records, start=2):
        sheet_item = str(row.get('Item name', '')).strip()
        if sheet_item in status_updates:
            new_status = status_updates[sheet_item]
            ws.update_cell(idx, 3, new_status)
    st.cache_data.clear()


# --- SIDEBAR: App Mode & Pantry Management ---
with st.sidebar:
    st.header("⚙️ App Mode")
    
    GUEST_MODE_LABEL = "👤 Quick Guest Mode"
    SELF_MODE_LABEL = "🔒 Admin / Self Mode (PIN Protected)"
    
    mode_selection = st.radio("Select Usage Mode", [GUEST_MODE_LABEL, SELF_MODE_LABEL])
    
    quick_guest_mode = True
    if mode_selection == SELF_MODE_LABEL:
        admin_pin = st.text_input("Enter Admin PIN:", type="password")
        # Checked securely against Streamlit secrets
        if admin_pin != st.secrets["ADMIN_PIN"]:
            if admin_pin != "":
                st.error("Incorrect PIN! Defaulting to Guest Mode.")
            quick_guest_mode = True
        else:
            quick_guest_mode = False
            st.success("Admin / Self Mode Unlocked!")
    else:
        quick_guest_mode = True

    st.markdown("---")
    
    if not quick_guest_mode:
        st.header("➕ Pantry Stock Management")
        all_records = fetch_pantry_full_records()
        veggies_list, low_stock_items, dynamic_categories = fetch_vegetables_and_categories()
        
        existing_items = sorted(list(set([str(r.get('Item name', '')).strip() for r in all_records if r.get('Item name')])))
        choice_options = ["➕ [Type New Item...]"] + existing_items
        
        with st.form("pantry_management_form", clear_on_submit=True):
            selected_item_choice = st.selectbox("Select Item or Add New", choice_options)
            
            if selected_item_choice == "➕ [Type New Item...]":
                new_category = st.selectbox("Category", dynamic_categories)
                item_name_input = st.text_input("New Item Name (e.g., Kovakkai)")
            else:
                item_name_input = selected_item_choice
                matched_row = next((r for r in all_records if str(r.get('Item name', '')).strip() == selected_item_choice), None)
                new_category = matched_row.get('Category', 'Vegetables') if matched_row else 'Vegetables'
                
            new_status = st.selectbox("Stock Status", ["In Stock", "Low", "Out of Stock"])
            
            submit_btn = st.form_submit_button("💾 Save to Sheet")
            if submit_btn:
                if item_name_input.strip():
                    try:
                        success, msg = upsert_pantry_item(new_category, item_name_input.strip(), new_status)
                        if success:
                            st.success(msg)
                    except Exception as ex:
                        st.error(f"Failed to update sheet: {ex}")
                else:
                    st.error("Please provide a valid item name.")

        st.markdown("---")
        st.subheader("🥦 Available Vegetables")
        if veggies_list:
            st.markdown(f"**Vegetables:** {', '.join(veggies_list)}")
        else:
            st.info("No vegetables currently marked as In Stock or Low.")
    else:
        st.info("👤 **Guest Mode Active**\n\nGoogle Sheet sync is bypassed. Enter your available fridge veggies directly on the main screen.")


# --- TABS LAYOUT ---
tab1, tab2, tab3 = st.tabs(["📅 Daily Menu Planner", "📦 Grocery & Stocking", "📊 Monthly Analytics"])

with tab1:
    st.subheader("Modular Daily Menu Builder")
    
    guest_veggies_input = ""
    if quick_guest_mode:
        st.info("💡 **Quick Guest Input:** Enter the vegetables you currently have in your fridge (standard Indian staples & spices are assumed available).")
        guest_veggies_input = st.text_input("Vegetables in Fridge Today (comma separated):", placeholder="e.g., Carrot, Potato, Beans, Tomato")
    
    with st.container():
        st.markdown("### ⚙️ Daily Kitchen Readiness")
        col_c1, col_c2, col_c3 = st.columns(3)
        with col_c1:
            batter_available = st.checkbox("Is Idli / Dosa Batter available today?", value=False)
        with col_c2:
            use_favorites = st.checkbox("Include Family Favorites?", value=True, help="Uncheck this to explore new regional dishes and varied recipes!")
        with col_c3:
            leftovers_input = st.text_input("Any Leftovers in Fridge to Finish?", placeholder="e.g., Sambar, cooked rice...")

    if st.button("✨ Generate Modular Menu"):
        with st.spinner("Quickly crafting your meal options..."):
            if quick_guest_mode:
                flat_veggies = guest_veggies_input if guest_veggies_input.strip() else "Standard mixed vegetables"
                low_str = "None specified"
                fruits_str = "Banana, Apple, Papaya, Seasonal Fruit"
                favorites = []
            else:
                veggies_list, low_stock_items, _ = fetch_vegetables_and_categories()
                available_fruits = fetch_available_fruits()
                flat_veggies = ", ".join(veggies_list) if veggies_list else "Standard veggies"
                low_str = ", ".join(low_stock_items) if low_stock_items else "None"
                fruits_str = ", ".join(available_fruits) if available_fruits else "Seasonal Fruit"
                favorites = fetch_family_favorites() if use_favorites else []
            
            fav_str = json.dumps(favorites[:15]) if favorites else "None specified"
            
            if use_favorites and not quick_guest_mode:
                fav_rule = f"6. Incorporate Family Favorites where applicable: {fav_str}."
            else:
                fav_rule = "6. DO NOT restrict menu to family favorites. Actively suggest diverse, creative Indian regional vegetarian dishes to encourage variety."

            batter_status_text = 'AVAILABLE (include Idli/Dosa)' if batter_available else 'NOT AVAILABLE (DO NOT suggest Idli or Dosa. Use Upma, Sevai, Pongal, Poori, Rava Dosa, Puttu, Adai, Appam)'
            leftovers_text = leftovers_input if leftovers_input else 'None'

            prompt = f"""
            You are an expert Indian culinary assistant. Generate a fast modular daily menu in strict JSON format:
            - 3 options for breakfast (simple names like main dish + side dish, separated by '+')
            - 3 options for lunch (simple names like main dish + side dish, separated by '+')
            - 3 options for dinner (simple names like main dish + side dish, separated by '+')
            - fruit (string): Pick EXACTLY ONE fruit from this available fruits list: [{fruits_str}]. If the list is empty, select a suitable seasonal Indian fruit.
            
            RULES:
            1. Pure Vegetarian cooking covering Tamil Nadu, South Indian, and North Indian regional delicacies.
            2. Batter status: {batter_status_text}.
            3. Leftovers to incorporate: {leftovers_text}.
            4. Available vegetables: {flat_veggies}.
            5. LOW STOCK ITEMS: [{low_str}]. CRITICAL: Ingredients marked as Low Stock must ONLY be used for Sambar, Kuzhambu, Rasam, Gravies, or Kootu. DO NOT assign them as primary vegetables for Poriyals/Sabzis which require large quantities.
            {fav_rule}
            
            JSON format required keys:
            'breakfast_options': array of 3 objects with keys ['option_id', 'name', 'veggies_used']
            'lunch_options': array of 3 objects with keys ['option_id', 'name', 'veggies_used']
            'dinner_options': array of 3 objects with keys ['option_id', 'name', 'veggies_used']
            'fruit': string
            """
            
            try:
                response = model.generate_content(
                    prompt, 
                    generation_config={"response_mime_type": "application/json"}
                )
                st.session_state.modular_options = json.loads(response.text)
                st.session_state.batch_plan = None
            except Exception as ex:
                if "429" in str(ex) or "Quota exceeded" in str(ex):
                    st.warning("⚠️ Free tier rate limit reached (5 requests/minute). Please wait about 30 seconds and click generate again!")
                else:
                    st.error(f"Error generating menu: {ex}")

    # --- DISPLAY OPTIONS ---
    if st.session_state.modular_options:
        mod = st.session_state.modular_options
        st.markdown("---")
        
        st.markdown("### 🍎 Fruit of the Day")
        default_fruit_val = mod.get('fruit', 'Banana')
        final_fruit_choice = st.text_input(
            "Gemini picked this fruit (edit below if you choose another):", 
            value=default_fruit_val, 
            key="fruit_text_input"
        )
        
        col_b, col_l, col_d = st.columns(3)
        
        with col_b:
            st.markdown("### 🌅 Breakfast")
            b_choices = {opt.get('name'): opt for opt in mod.get('breakfast_options', [])}
            b_choices["[Custom / Out of Box]"] = "custom"
            selected_b_name = st.selectbox("Select Breakfast", list(b_choices.keys()), key="sel_b")
            
            if selected_b_name == "[Custom / Out of Box]":
                custom_b_main = st.text_input("Custom Breakfast Main", "Upma")
                custom_b_side = st.text_input("Custom Breakfast Side", "Chutney")
                selected_b = {"name": f"{custom_b_main} + {custom_b_side}", "veggies_used": []}
            else:
                selected_b = b_choices[selected_b_name]

        with col_l:
            st.markdown("### 🍱 Lunch")
            l_choices = {opt.get('name'): opt for opt in mod.get('lunch_options', [])}
            l_choices["[Custom / Out of Box]"] = "custom"
            selected_l_name = st.selectbox("Select Lunch", list(l_choices.keys()), key="sel_l")
            
            if selected_l_name == "[Custom / Out of Box]":
                custom_l_main = st.text_input("Custom Lunch Main", "Sambar & Rasam")
                custom_l_side = st.text_input("Custom Lunch Side", "Poriyal")
                selected_l = {"name": f"{custom_l_main} + {custom_l_side}", "veggies_used": []}
            else:
                selected_l = l_choices[selected_l_name]

        with col_d:
            st.markdown("### 🌙 Dinner")
            d_choices = {opt.get('name'): opt for opt in mod.get('dinner_options', [])}
            d_choices["[Custom / Out of Box]"] = "custom"
            selected_d_name = st.selectbox("Select Dinner", list(d_choices.keys()), key="sel_d")
            
            if selected_d_name == "[Custom / Out of Box]":
                custom_d_main = st.text_input("Custom Dinner Main", "Chapathi")
                custom_d_side = st.text_input("Custom Dinner Side", "Curry")
                selected_d = {"name": f"{custom_d_main} + {custom_d_side}", "veggies_used": []}
            else:
                selected_d = d_choices[selected_d_name]

        # YouTube Links
        st.markdown("---")
        st.markdown("### ▶️ Recipe Links")
        yt_b = urllib.parse.quote(selected_b.get('name', '') + " recipe")
        yt_l = urllib.parse.quote(selected_l.get('name', '') + " recipe")
        yt_d = urllib.parse.quote(selected_d.get('name', '') + " recipe")
        
        col_yt1, col_yt2, col_yt3 = st.columns(3)
        with col_yt1:
            st.markdown(f"[Breakfast Recipe]({f'https://www.youtube.com/results?search_query={yt_b}'})")
        with col_yt2:
            st.markdown(f"[Lunch Recipe]({f'https://www.youtube.com/results?search_query={yt_l}'})")
        with col_yt3:
            st.markdown(f"[Dinner Recipe]({f'https://www.youtube.com/results?search_query={yt_d}'})")

        all_veggies = list(set(
            selected_b.get('veggies_used', []) + 
            selected_l.get('veggies_used', []) + 
            selected_d.get('veggies_used', [])
        ))

        if st.button("📋 Prepare Final Daily Plan for Confirmation"):
            def parse_dish_parts(dish_str):
                dish_str = str(dish_str).strip()
                if "+" in dish_str:
                    parts = dish_str.split("+", 1)
                elif " with " in dish_str.lower():
                    parts = re.split(r'\swith\s', dish_str, flags=re.IGNORECASE, maxsplit=1)
                else:
                    parts = [dish_str, ""]
                return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""

            b_main, b_side = parse_dish_parts(selected_b.get('name', ''))
            l_main, l_side = parse_dish_parts(selected_l.get('name', ''))
            d_main, d_side = parse_dish_parts(selected_d.get('name', ''))
            
            st.session_state.pending_log = {
                "date": str(datetime.date.today()),
                "tiffin_morning": b_main,
                "tiffin_morning_side": b_side,
                "lunch_main": l_main,
                "lunch_side": l_side,
                "tiffin_dinner": d_main,
                "tiffin_dinner_side": d_side,
                "fruit": final_fruit_choice.strip() if final_fruit_choice.strip() else "Seasonal Fruit",
                "veggies_used": all_veggies
            }
            st.success("Daily plan prepared below for review!")

    # --- FINAL CONFIRMATION & STOCK ADJUSTMENT ---
    if st.session_state.pending_log:
        st.markdown("---")
        st.subheader("📋 End-of-Day / Selection Confirmation & Stock Adjustment")
        p = st.session_state.pending_log
        st.json(p)
        
        depletion_updates = {}
        if not quick_guest_mode:
            st.markdown("### 🎛️ Interactive Pantry Stock Adjustment")
            st.markdown("Review ingredients used in today's menu and adjust their stock levels before saving:")
            if p.get("veggies_used"):
                for veg in p.get("veggies_used"):
                    depletion_updates[veg] = st.selectbox(
                        f"Status for '{veg}':", 
                        ["In Stock", "Low", "Out of Stock"], 
                        index=2, 
                        key=f"depletion_{veg}"
                    )
            else:
                st.info("No specific pantry veggies tracked for this meal combination.")
        else:
            st.info("Guest Mode active: Skipping automated sheet stock updates.")
        
        col_save, col_cancel = st.columns(2)
        with col_save:
            if st.button("✅ Confirm & Save"):
                try:
                    st.session_state.history_db.append(p)
                    if not quick_guest_mode:
                        save_meal_history_to_sheet(p)
                        if depletion_updates:
                            update_multiple_item_statuses(depletion_updates)
                        st.success("Successfully saved to Meal_History sheet and updated pantry stock levels!")
                    else:
                        st.success("Confirmed! (Guest Mode: saved to session memory only).")
                    
                    batch_prompt = f"""
                    Based on today's confirmed menu:
                    - Breakfast: {p.get('tiffin_morning')} + {p.get('tiffin_morning_side')}
                    - Lunch: {p.get('lunch_main')} + {p.get('lunch_side')}
                    - Dinner: {p.get('tiffin_dinner')} + {p.get('tiffin_dinner_side')}
                    
                    Provide a concise, practical Batch Cooking Action Plan detailing what to batch cook right now (e.g., making extra sambar/gravy in the morning for evening/dinner, chopping extra veggies, soaking/grinding notes). Format in clean markdown bullet points.
                    """
                    b_res = model.generate_content(batch_prompt)
                    st.session_state.batch_plan = b_res.text
                    st.session_state.pending_log = None
                except Exception as ex:
                    st.error(f"Error saving plan: {ex}")
        with col_cancel:
            if st.button("❌ Discard"):
                st.session_state.pending_log = None
                st.info("Discarded pending log.")

    if st.session_state.batch_plan:
        st.markdown("---")
        st.markdown("### 🔄 Today's Batch Cooking Action Plan")
        st.info(st.session_state.batch_plan)

with tab2:
    st.subheader("📦 Grocery & Stocking Advisor")
    if quick_guest_mode:
        st.info("👤 Guest Mode is active. Grocery Advisor relies on your Google Sheet inventory. Switch to Admin / Self Mode in the sidebar with your PIN to use this tab.")
    else:
        st.markdown("Generates a smart shopping list based on your actual pantry stock and items marked as **Low** or **Out of Stock**.")
        if st.button("Analyze Pantry & Get Shopping List"):
            with st.spinner("Analyzing your full pantry inventory..."):
                all_pantry_records = fetch_pantry_full_records()
                
                pantry_status_summary = ", ".join([
                    f"{r.get('Item name')} [Status: {r.get('Status', 'Unknown')}, Category: {r.get('Category', 'General')}]" 
                    for r in all_pantry_records if r.get('Item name') and str(r.get('Item name')).lower() != 'nan'
                ])
                
                grocery_prompt = f"""
                You are an expert Indian household kitchen assistant. 
                Here is my complete pantry inventory with current stock statuses:
                [{pantry_status_summary if pantry_status_summary else 'No items recorded yet'}]
                
                TASK:
                Analyze this inventory. Generate a targeted grocery shopping list focusing primarily on items marked as 'Out of Stock' or 'Low', alongside essential weekly replenishments for Indian vegetarian cooking (such as fresh vegetables, seasonal fruits, lentils, staples, and spices). 
                
                Format the output in clean, categorized markdown bullet points.
                """
                
                try:
                    res = model.generate_content(grocery_prompt)
                    st.session_state.grocery_list = res.text
                except Exception as ex:
                    st.error(f"Error generating shopping list: {ex}")

        if st.session_state.grocery_list:
            st.markdown("---")
            st.markdown(st.session_state.grocery_list)

with tab3:
    st.subheader("📊 Monthly Health & Variety Analytics")
    if len(st.session_state.history_db) > 0:
        df = pd.DataFrame(st.session_state.history_db)
        st.dataframe(df)
        st.metric(label="Logged Meals Count", value=len(df))
    else:
        st.info("No meals logged yet. Confirm and save a daily plan to view analytics here.")
