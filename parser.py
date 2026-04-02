import os
import json
import pandas as pd
import glob
import math

def is_valid(val):
    if pd.isna(val):
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    return True

def str_val(val, default=""):
    if not is_valid(val):
        return default
    if isinstance(val, (int, float)):
        # Remove .0 if it's an integer stored as float
        if int(val) == val:
            return str(int(val))
    return str(val).strip()

def main():
    base_dir = "data"
    output_file = "shopee_products.json"

    # Initialize the main dictionary
    products = {}
    # 優化：使用字典索引加速型號查找 (O(1) vs O(n))
    model_index = {}  # {product_id: {model_name: model_data}}

    print("Parsing 主庫存 (Main Stock) files...")
    stock_files = glob.glob(os.path.join(base_dir, "主庫存", "*.xlsx"))
    for file_path in stock_files:
        try:
            df = pd.read_excel(file_path, engine="calamine", header=None)

            # Find the row that contains '商品 ID' in the first column to set as headers
            header_row_idx = None
            for i in range(min(5, len(df))):
                val = str(df.iloc[i, 0]).strip()
                if val == "商品 ID" or val == "et_title_product_id":
                    header_row_idx = i
                    break

            if header_row_idx is not None:
                df.columns = df.iloc[header_row_idx]
                df = df.iloc[header_row_idx+1:].reset_index(drop=True)
            else:
                df = pd.read_excel(file_path, engine="calamine", header=1)

            # Cache column name resolution (avoid repeated lookups)
            prod_id_col = "商品 ID" if "商品 ID" in df.columns else "et_title_product_id"
            prod_name_col = "商品名稱" if "商品名稱" in df.columns else "et_title_product_name"
            var_name_col = "商品規格名稱" if "商品規格名稱" in df.columns else ("et_title_variation_name" if "et_title_variation_name" in df.columns else "商品選項名稱")
            var_sku_col = "商品選項 ID" if "商品選項 ID" in df.columns else ("et_title_variation_id" if "et_title_variation_id" in df.columns else "商品規格 ID")
            stock_col = "庫存" if "庫存" in df.columns else ("et_title_variation_stock" if "et_title_variation_stock" in df.columns else "商品庫存")

            for _, row in df.iterrows():
                p_id = str_val(row.get(prod_id_col))
                if not p_id or p_id == "nan" or p_id == "商品 ID" or p_id == "sales_info":
                    continue

                p_name = str_val(row.get(prod_name_col))
                v_name = str_val(row.get(var_name_col))
                v_id = str_val(row.get(var_sku_col))
                stock = str_val(row.get(stock_col))

                if p_id not in products:
                    products[p_id] = {
                        "商品名稱": p_name,
                        "已售出總數量": "0",
                        "商品圖片網址": "",
                        "型號": [],
                        "總月銷量": "0"
                    }
                    model_index[p_id] = {}  # Initialize index for this product

                # 優化：使用字典索引代替線性查找
                if v_name and v_name not in model_index[p_id]:
                    model_data = {
                        "型號名稱": v_name,
                        "已售出數量": "0",
                        "商品庫存": stock,
                        "商品選項 ID": v_id,
                        "型號圖片網址": "",
                        "月銷量": "0"
                    }
                    products[p_id]["型號"].append(model_data)
                    model_index[p_id][v_name] = model_data

        except Exception as e:
            print(f"Error parsing {file_path}: {e}")

    print("Parsing parentskudetail (Monthly Sales) file...")
    sales_files = glob.glob(os.path.join(base_dir, "parentskudetail*.xlsx"))
    if sales_files:
        try:
            df_sales = pd.read_excel(sales_files[0], engine="calamine")
            for _, row in df_sales.iterrows():
                p_id = str_val(row.get("商品 ID"))
                if not p_id or p_id == "nan" or p_id == "商品 ID":
                    continue

                v_name = str_val(row.get("商品規格"))
                monthly_sales = str_val(row.get("商品件數 (可出貨訂單)"), default="0")
                if monthly_sales == "-":
                    monthly_sales = "0"

                # If product doesn't exist in stock files, initialize it
                if p_id not in products:
                    p_name = str_val(row.get("商品名稱"))
                    products[p_id] = {
                        "商品名稱": p_name,
                        "已售出總數量": "0",
                        "商品圖片網址": "",
                        "型號": [],
                        "總月銷量": "0"
                    }
                    model_index[p_id] = {}

                if v_name == "-":
                    # Total monthly sales for the product
                    products[p_id]["總月銷量"] = monthly_sales
                else:
                    # 優化：使用字典索引查找型號
                    if v_name in model_index[p_id]:
                        model_index[p_id][v_name]["月銷量"] = monthly_sales
                    else:
                        # Model doesn't exist, create it
                        model_data = {
                            "型號名稱": v_name,
                            "已售出數量": "0",
                            "商品庫存": "0",
                            "商品選項 ID": "",
                            "型號圖片網址": "",
                            "月銷量": monthly_sales
                        }
                        products[p_id]["型號"].append(model_data)
                        model_index[p_id][v_name] = model_data
        except Exception as e:
            print(f"Error parsing sales file: {e}")

    print("Parsing media_info (Images) file...")
    media_files = glob.glob(os.path.join(base_dir, "*media_info*.xlsx"))
    if media_files:
        try:
            df_media = pd.read_excel(media_files[0], engine="calamine")
            for _, row in df_media.iterrows():
                p_id = str_val(row.get("et_title_product_id"))
                if not p_id or not p_id.isdigit():
                    continue

                if p_id in products:
                    cover_img = str_val(row.get("ps_item_cover_image"))
                    if cover_img and cover_img.startswith("http"):
                        products[p_id]["商品圖片網址"] = cover_img

                    # Iterate up to 50 options
                    for i in range(1, 51):
                        opt_name_col = f"et_title_option_{i}_for_variation_1"
                        opt_img_col = f"et_title_option_image_{i}_for_variation_1"

                        if opt_name_col in row and opt_img_col in row:
                            opt_name = str_val(row.get(opt_name_col))
                            opt_img = str_val(row.get(opt_img_col))

                            # Skip placeholder texts
                            if not opt_name or opt_name == "不可編輯" or "選項" in opt_name:
                                continue

                            if opt_img and opt_img.startswith("http"):
                                # 優化：使用字典索引查找型號
                                if opt_name in model_index.get(p_id, {}):
                                    model_index[p_id][opt_name]["型號圖片網址"] = opt_img
        except Exception as e:
            print(f"Error parsing media info: {e}")

    # Write out the JSON result
    print(f"Writing parsed data to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False, indent=4)

    print("Parsing complete! Processed", len(products), "products.")

if __name__ == "__main__":
    main()
