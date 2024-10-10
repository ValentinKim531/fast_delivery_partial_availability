import os
import httpx
import math
from fastapi import FastAPI, Request
import logging
from fastapi.middleware.cors import CORSMiddleware
import json
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

URL_SEARCH = os.getenv("URL_SEARCH")
URL_PRICE = os.getenv("URL_PRICE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/partial_availability")
async def main_process(request: Request):
    request_data = await request.json()
    encoded_city = request_data.get("city")
    sku_data = request_data.get("skus", [])
    address = request_data.get("address", {})

    user_lat = request_data.get("address", {}).get("lat")
    user_lon = request_data.get("address", {}).get("lng")

    if not encoded_city or not sku_data or user_lat is None or user_lon is None:
        return {"error": "City, SKU data, and user coordinates are required"}

    # Первый SKU приоритетный
    priority_sku = sku_data[0]["sku"]

    payload = [{"sku": item["sku"], "count_desired": item["count_desired"]} for item in sku_data]

    # Поиск лекарств в аптеках
    pharmacies = await find_medicines_in_pharmacies(encoded_city, payload)

    # logger.info(f"Pharmacies found: {pharmacies}")

    # Поиск аптек с учетом наличия приоритетного товара
    priority_pharmacies = await filter_pharmacies_with_priority(pharmacies, priority_sku)

    if not priority_pharmacies.get("filtered_pharmacies"):
        # Если нет аптек с приоритетным товаром, продолжаем с неполным набором
        partial_pharmacies = await filter_pharmacies_with_priority_and_analogs(pharmacies, priority_sku)
    else:
        # Если аптеки с приоритетным товаром есть, фильтруем с учетом аналогов
        partial_pharmacies = await filter_pharmacies_with_priority_and_analogs(priority_pharmacies, priority_sku)

    # Сортировка по наибольшему количеству доступных товаров
    top_pharmacies = await sort_pharmacies_by_fulfillment(partial_pharmacies)

    # Выбор ближайших и самых дешевых аптек
    closest_pharmacies = await get_top_closest_pharmacies(top_pharmacies, user_lat, user_lon)
    save_response_to_file(closest_pharmacies, file_name='data4_top_closest_pharmacies.json')

    cheapest_pharmacies = await get_top_cheapest_pharmacies(top_pharmacies)
    save_response_to_file(cheapest_pharmacies, file_name='data4_top_cheapest_pharmacies.json')


    # Расчет вариантов доставки
    delivery_options1 = await get_delivery_options(closest_pharmacies, user_lat, user_lon)
    save_response_to_file(delivery_options1, file_name='data5_delivery_options_closest.json')

    delivery_options2 = await get_delivery_options(cheapest_pharmacies, user_lat, user_lon)
    save_response_to_file(delivery_options2, file_name='data5_delivery_options_cheapest.json')

    result = await best_option(delivery_options1, delivery_options2)
    save_response_to_file(result, file_name='data6_final_result.json')
    return result


# Поиск лекарств в аптеках
async def find_medicines_in_pharmacies(encoded_city, payload):
    async with httpx.AsyncClient() as client:
        response = await client.post(URL_SEARCH, params=encoded_city, json=payload)
        response.raise_for_status()
        save_response_to_file(response.json(), file_name='data1_found_all.json')
        return response.json()


# Фильтр аптек с учетом приоритетного товара
async def filter_pharmacies_with_priority(pharmacies, priority_sku):
    filtered_pharmacies = []

    for pharmacy in pharmacies.get("result", []):
        products = pharmacy.get("products", [])

        # Проверяем наличие приоритетного лекарства с ненулевым количеством
        has_priority_sku = any(product["sku"] == priority_sku and product["quantity"] != 0 for product in products)

        # Если приоритетное лекарство найдено с ненулевым количеством, добавляем аптеку
        if has_priority_sku:
            filtered_pharmacies.append(pharmacy)
        else:
            # Если приоритетное лекарство найдено, но его количество равно 0
            for product in products:
                if product["sku"] == priority_sku and product["quantity"] == 0:
                    # Проверяем наличие аналогов с ненулевым количеством
                    has_valid_analog = any(analog["quantity"] != 0 for analog in product.get("analogs", []))
                    if has_valid_analog:
                        filtered_pharmacies.append(pharmacy)
                        break  # Прекращаем поиск после нахождения подходящего аналога

    # Логирование результатов перед сохранением
    print(f"Найдено аптек с приоритетным товаром или аналогом: {len(filtered_pharmacies)}")
    save_response_to_file(filtered_pharmacies, file_name='data2_found_with_priority.json')
    return {"filtered_pharmacies": filtered_pharmacies}


async def filter_pharmacies_with_priority_and_analogs(pharmacies, priority_sku):
    pharmacies_with_replacements = []

    # Проходим по аптекам
    for pharmacy in pharmacies.get("filtered_pharmacies") or pharmacies.get("result", []):
        products = pharmacy.get("products", [])
        updated_products = []  # Обновленный список товаров (с аналогами и приоритетным товаром)
        total_sum = 0  # Общая сумма заказа
        replacements_needed = 0  # Количество замен
        replaced_skus = []  # Список замененных товаров
        priority_included = False  # Флаг для приоритетного товара


        for product in products:
            if product["sku"] == priority_sku:
                # Обрабатываем приоритетный товар
                if product["quantity"] >= product["quantity_desired"]:
                    product_total_price = product["base_price"] * product["quantity_desired"]
                    total_sum += product_total_price
                    updated_products.append(product)
                    priority_included = True
                else:
                    # Проверяем наличие аналога для приоритетного товара
                    cheapest_analog = min(product.get("analogs", []), key=lambda analog: analog["base_price"], default=None)
                    if cheapest_analog:
                        replacement_product = {
                            "source_code": cheapest_analog["source_code"],
                            "sku": cheapest_analog["sku"],
                            "name": cheapest_analog["name"],
                            "base_price": cheapest_analog["base_price"],
                            "price_with_warehouse_discount": cheapest_analog["price_with_warehouse_discount"],
                            "warehouse_discount": cheapest_analog["warehouse_discount"],
                            "quantity": cheapest_analog["quantity"],
                            "quantity_desired": product["quantity_desired"],
                            "diff": product["diff"],
                            "avg_price": product["avg_price"],
                            "min_price": product["min_price"],
                            "pp_packing": cheapest_analog.get("pp_packing", ""),
                            "manufacturer_id": cheapest_analog.get("manufacturer_id", ""),
                            "recipe_needed": cheapest_analog.get("recipe_needed", False),
                            "strong_recipe": cheapest_analog.get("strong_recipe", False),
                        }
                        analog_total_price = cheapest_analog["base_price"] * product["quantity_desired"]
                        total_sum += analog_total_price
                        updated_products.append(replacement_product)
                        replacements_needed += 1
                        replaced_skus.append({
                            "original_sku": product["sku"],
                            "replacement_sku": cheapest_analog["sku"]
                        })
                        priority_included = True
                    else:
                        logger.info(f"Нет аналога для приоритетного товара {product['sku']}, продолжаем без него.")

            else:
                # Если товар не является приоритетным, обрабатываем его стандартной логикой
                if product["quantity"] >= product["quantity_desired"]:
                    product_total_price = product["base_price"] * product["quantity_desired"]
                    total_sum += product_total_price
                    updated_products.append(product)
                else:
                    # Проверяем наличие аналогов для обычного товара
                    cheapest_analog = min(product.get("analogs", []), key=lambda analog: analog["base_price"], default=None)
                    if cheapest_analog:
                        replacement_product = {
                            "source_code": cheapest_analog["source_code"],
                            "sku": cheapest_analog["sku"],
                            "name": cheapest_analog["name"],
                            "base_price": cheapest_analog["base_price"],
                            "price_with_warehouse_discount": cheapest_analog["price_with_warehouse_discount"],
                            "warehouse_discount": cheapest_analog["warehouse_discount"],
                            "quantity": cheapest_analog["quantity"],
                            "quantity_desired": product["quantity_desired"],
                            "diff": product["diff"],
                            "avg_price": product["avg_price"],
                            "min_price": product["min_price"],
                            "pp_packing": cheapest_analog.get("pp_packing", ""),
                            "manufacturer_id": cheapest_analog.get("manufacturer_id", ""),
                            "recipe_needed": cheapest_analog.get("recipe_needed", False),
                            "strong_recipe": cheapest_analog.get("strong_recipe", False),
                        }
                        analog_total_price = cheapest_analog["base_price"] * product["quantity_desired"]
                        total_sum += analog_total_price
                        updated_products.append(replacement_product)
                        replacements_needed += 1
                        replaced_skus.append({
                            "original_sku": product["sku"],
                            "replacement_sku": cheapest_analog["sku"]
                        })
                    else:
                        logger.info(f"Нет аналогов для товара {product['sku']}, пропускаем.")

        # Добавляем аптеку в список только если есть хотя бы один товар
        if updated_products:
            pharmacies_with_replacements.append({
                "pharmacy": {
                    "source": pharmacy["source"],
                    "products": updated_products,
                    "total_sum": total_sum,
                    "replacements_needed": replacements_needed,
                    "replaced_skus": replaced_skus
                }
            })

    print(f"Найдено аптек с заменами: {len(pharmacies_with_replacements)}")
    save_response_to_file(pharmacies_with_replacements, file_name='data2_pharmacies_with_replacements.json')
    return {"filtered_pharmacies": pharmacies_with_replacements}



# Сортировка аптек по количеству доступных товаров и выбор аптек с наибольшей корзиной
async def sort_pharmacies_by_fulfillment(pharmacies_with_partial_availability):
    # Группируем аптеки по количеству доступных товаров в корзине
    grouped_pharmacies = defaultdict(list)

    for pharmacy in pharmacies_with_partial_availability.get("filtered_pharmacies", []):
        # Получаем количество товаров в корзине для каждой аптеки
        num_products = len(pharmacy["pharmacy"].get("products", []))
        grouped_pharmacies[num_products].append(pharmacy)

    # Находим максимальное количество товаров в корзине
    max_products = max(grouped_pharmacies.keys(), default=0)

    # Берем все аптеки, у которых это максимальное количество товаров
    top_pharmacies = grouped_pharmacies[max_products]

    # Логируем количество аптек и товаров
    print(f"Выбрано {len(top_pharmacies)} аптек с максимальной корзиной из {max_products} товаров")

    # Сохраняем результат в файл для отладки
    save_response_to_file(top_pharmacies, file_name='data3_sorted_pharmacies.json')

    return {"list_pharmacies": top_pharmacies}


# Функция для выбора ближайших аптек
async def get_top_closest_pharmacies(pharmacies, user_lat, user_lon):
    pharmacies_with_distance = []
    for pharmacy in pharmacies.get("list_pharmacies", []):
        source_info = pharmacy.get("pharmacy", {}).get("source", {})
        pharmacy_lat = source_info.get("lat")
        pharmacy_lon = source_info.get("lon")

        distance = haversine_distance(user_lat, user_lon, pharmacy_lat, pharmacy_lon)
        pharmacies_with_distance.append({"pharmacy": pharmacy, "distance": distance})

    sorted_pharmacies = sorted(pharmacies_with_distance, key=lambda x: x["distance"])
    save_response_to_file(sorted_pharmacies, file_name='data3_sorted_distance.json')
    closest_pharmacies = [item["pharmacy"] for item in sorted_pharmacies[:2]]
    return {"list_pharmacies": closest_pharmacies}


# Функция для выбора самых дешевых аптек
async def get_top_cheapest_pharmacies(pharmacies):
    sorted_pharmacies = sorted(
        pharmacies.get("list_pharmacies", []),
        key=lambda x: x["pharmacy"].get("total_sum", float('inf')) if "pharmacy" in x else float('inf')
    )
    save_response_to_file(sorted_pharmacies, file_name='data3_sorted_cheapest.json')
    return {"list_pharmacies": sorted_pharmacies[:3]}


# Алгоритм расчета расстояния
def haversine_distance(lat1, lon1, lat2, lon2):
    return math.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2)


async def get_delivery_options(pharmacies, user_lat, user_lon):
    cheapest_option = None
    fastest_option = None

    for pharmacy in pharmacies["list_pharmacies"]:
        source = pharmacy.get("pharmacy", {}).get("source", {})
        products = pharmacy.get("pharmacy", {}).get("products", [])

        if "code" not in source:
            continue

        pharmacy_total_sum = pharmacy.get("pharmacy", {}).get("total_sum", 0)

        # Формирование списка товаров с учетом оригиналов и аналогов
        items = []
        for product in products:
            if product["quantity"] >= product["quantity_desired"]:
                # Если товар в наличии, добавляем его в итоговый список
                items.append({"sku": product["sku"], "quantity": product["quantity_desired"]})
            elif "analogs" in product and product["analogs"]:
                # Если товара нет, ищем самый дешевый аналог
                cheapest_analog = min(product["analogs"], key=lambda analog: analog["base_price"])
                items.append({"sku": cheapest_analog["sku"], "quantity": product["quantity_desired"]})

        if not items:
            continue

        # Формируем запрос для расчета доставки
        payload = {
            "items": items,  # Добавляем оригиналы или аналоги
            "dst": {
                "lat": user_lat,
                "lng": user_lon
            },
            "source_code": source["code"]
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(URL_PRICE, json=payload)
                response.raise_for_status()
                delivery_data = response.json()

                if delivery_data.get("status") == "success":
                    delivery_options = delivery_data["result"]["delivery"]

                    # Сравнение по самой дешевой опции
                    for option in delivery_options:
                        total_price = pharmacy_total_sum + option["price"]  # Цена товаров + цена доставки
                        if cheapest_option is None or total_price < cheapest_option["total_price"]:
                            cheapest_option = {
                                "pharmacy": pharmacy,
                                "total_price": total_price,
                                "delivery_option": option
                            }

                        # Сравнение по самой быстрой доставке
                        if fastest_option is None or option["eta"] < fastest_option["delivery_option"]["eta"]:
                            fastest_option = {
                                "pharmacy": pharmacy,
                                "total_price": total_price,
                                "delivery_option": option
                            }
            except httpx.RequestError as e:
                print(f"An error occurred while requesting {'URL_PRICE'}: {e}")
            except httpx.HTTPStatusError as e:
                print(f"Error response {e.response.status_code} while requesting {'URL_PRICE'}: {e}")

    return {
        "cheapest_delivery_option": cheapest_option,
        "fastest_delivery_option": fastest_option
    }


# Функция для сравнения вариантов
async def best_option(var1, var2):
    # Проверяем, что оба варианта имеют данные перед сравнением
    best_cheapest_option = None
    best_fastest_option = None

    # Если оба варианта имеют данные по цене, сравниваем их
    if var1.get("cheapest_delivery_option") and var2.get("cheapest_delivery_option"):
        best_cheapest_option = min(var1["cheapest_delivery_option"], var2["cheapest_delivery_option"],
                                   key=lambda x: x["total_price"])
    elif var1.get("cheapest_delivery_option"):
        best_cheapest_option = var1["cheapest_delivery_option"]
    elif var2.get("cheapest_delivery_option"):
        best_cheapest_option = var2["cheapest_delivery_option"]

    # Если оба варианта имеют данные по времени доставки, сравниваем их
    if var1.get("fastest_delivery_option") and var2.get("fastest_delivery_option"):
        best_fastest_option = min(var1["fastest_delivery_option"], var2["fastest_delivery_option"],
                                  key=lambda x: x["delivery_option"]["eta"])
    elif var1.get("fastest_delivery_option"):
        best_fastest_option = var1["fastest_delivery_option"]
    elif var2.get("fastest_delivery_option"):
        best_fastest_option = var2["fastest_delivery_option"]

    return {
        "best_cheapest_option": best_cheapest_option,
        "best_fastest_option": best_fastest_option
    }



def save_response_to_file(data, file_name='data.json'):
    try:
        # Сохраняем данные в файл
        with open(file_name, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=4)

        print(f"Данные успешно сохранены в файл: {file_name}")
    except Exception as e:
        print(f"Ошибка при сохранении данных: {e}")