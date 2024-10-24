import os
import httpx
import math
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging
from fastapi.middleware.cors import CORSMiddleware
import json
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime, timedelta
import pytz


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

    all_delivery_options = delivery_options1 + delivery_options2
    save_response_to_file(all_delivery_options, file_name='data5_all_delivery_options.json')

    result = await best_option(all_delivery_options)
    save_response_to_file(result, file_name='data6_final_result.json')
    return result



async def find_medicines_in_pharmacies(encoded_city, payload):
    async with httpx.AsyncClient() as client:
        response = await client.post(URL_SEARCH, params=encoded_city, json=payload)
        response.raise_for_status()
        save_response_to_file(response.json(), file_name='data1_found_all.json')
        return response.json()

# мок для тестирования локальных результатов поиска
# async def find_medicines_in_pharmacies(encoded_city, payload):
#     async with httpx.AsyncClient() as client:
#         response = await client.get("http://localhost:8003/search_medicines")
#         response.raise_for_status()  # Проверка на ошибки
#         data = response.json()  # Получаем JSON
#         save_response_to_file(data, file_name='data1_found_all.json')
#         return data  # Возвращаем JSON данные


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

        for product in products:
            if product["sku"] == priority_sku:
                # Обрабатываем приоритетный товар
                if product["quantity"] >= product["quantity_desired"]:
                    product_total_price = product["base_price"] * product["quantity_desired"]
                    total_sum += product_total_price
                    updated_products.append(product)
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

    logger.info(f"Найдено аптек с заменами: {len(pharmacies_with_replacements)}")
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
    logger.info(f"Выбрано {len(top_pharmacies)} аптек с максимальной корзиной из {max_products} товаров")

    # Сохраняем результат в файл для отладки
    save_response_to_file(top_pharmacies, file_name='data3_sorted_pharmacies.json')

    return {"list_pharmacies": top_pharmacies}


# Функция для выбора ближайших 2 аптек
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


# Функция для выбора самых дешевых 3 аптек
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


def is_pharmacy_open_soon(closes_at, opening_hours):
    """Проверяет, закроется ли аптека через 1 час или позже, или если аптека работает круглосуточно."""
    almaty_tz = pytz.timezone('Asia/Almaty')
    current_time = datetime.now(almaty_tz)

    # мок для тестов локальных результатов поиска
    # current_time = almaty_tz.localize(datetime(2024, 10, 21, 22, 30, 0))

    # Проверка, если аптека круглосуточная
    if opening_hours == "Круглосуточно":
        return False  # Круглосуточная аптека не закроется скоро

    # Парсим время закрытия аптеки
    closes_time = datetime.strptime(closes_at, "%Y-%m-%dT%H:%M:%SZ")
    closes_time = closes_time.replace(tzinfo=pytz.UTC).astimezone(almaty_tz)
    # Если аптека закрывается через 1 час или меньше
    return closes_time - current_time <= timedelta(hours=1)


def is_pharmacy_closed(closes_at, opening_hours):
    """Проверяет, закрыта ли аптека на момент запроса."""
    almaty_tz = pytz.timezone('Asia/Almaty')
    current_time = datetime.now(almaty_tz)

    # мок для тестов локальных результатов поиска
    # current_time = almaty_tz.localize(datetime(2024, 10, 21, 22, 30, 0))

    # Проверка, если аптека круглосуточная
    if opening_hours == "Круглосуточно":
        return False  # Круглосуточная аптека никогда не закрыта

    closes_time = datetime.strptime(closes_at, "%Y-%m-%dT%H:%M:%SZ")
    closes_time = closes_time.replace(tzinfo=pytz.UTC).astimezone(almaty_tz)
    return current_time >= closes_time  # Если текущее время уже позже закрытия


async def get_delivery_options(pharmacies, user_lat, user_lon):
    """Функция возвращает все данные о доставке для аптек без принятия решений."""
    results = []

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
                items.append({"sku": product["sku"], "quantity": product["quantity_desired"]})
            elif "analogs" in product and product["analogs"]:
                cheapest_analog = min(product["analogs"], key=lambda analog: analog["base_price"])
                items.append({"sku": cheapest_analog["sku"], "quantity": product["quantity_desired"]})

        if not items:
            continue

        # Формируем запрос для расчета доставки
        payload = {
            "items": items,
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
                print(delivery_data)

                if delivery_data.get("status") == "success":
                    delivery_options = delivery_data["result"]["delivery"]

                    for option in delivery_options:
                        results.append({
                            "pharmacy": pharmacy,
                            "total_price": pharmacy_total_sum + option["price"],
                            "delivery_option": option
                        })

            except httpx.RequestError as e:
                print(f"An error occurred while requesting {'URL_PRICE'}: {e}")
            except httpx.HTTPStatusError as e:
                print(f"Error response {e.response.status_code} while requesting {'URL_PRICE'}: {e}")

    return results


async def best_option(delivery_data):
    """Функция для сравнения аптек и выбора лучших опций с учетом времени закрытия, цены и условий."""
    cheapest_open_pharmacy = None
    cheapest_closed_pharmacy = None
    alternative_cheapest_option = None

    fastest_open_pharmacy = None
    fastest_closed_pharmacy = None
    alternative_fastest_option = None

    pharmacy_closes_soon = False
    pharmacy_closed = False

    for option in delivery_data:
        pharmacy = option["pharmacy"]
        source = pharmacy.get("pharmacy", {}).get("source", {})
        closes_at = source.get("closes_at")
        opening_hours = source.get("opening_hours", "")

        # Логика для проверки закрытых аптек и аптек, которые закроются через час
        if closes_at:
            pharmacy_closes_soon = is_pharmacy_open_soon(closes_at, opening_hours)
            pharmacy_closed = is_pharmacy_closed(closes_at, opening_hours)

        logger.info(
            f"Step 1: Checking pharmacy: {source['code']}, closes_at: {closes_at}, pharmacy_closes_soon: {pharmacy_closes_soon}, pharmacy_closed: {pharmacy_closed}, total_price: {option['total_price']}, eta: {option['delivery_option']['eta']}"
        )

        # Логика для самой дешевой аптеки
        if not pharmacy_closed:
            # Сохраняем самую дешевую открытую аптеку
            if cheapest_open_pharmacy is None or option["total_price"] < cheapest_open_pharmacy["total_price"]:
                logger.info(
                    f"Step 2: Setting cheapest_open_pharmacy to {source['code']} with total_price {option['total_price']}")
                cheapest_open_pharmacy = option
                # Если аптека не закрывается скоро, не нужно альтернативы
                if not pharmacy_closes_soon:
                    logger.info(
                        f"Step 3: Pharmacy {source['code']} works longer than 1 hour, resetting alternative_cheapest_option to None")
                    alternative_cheapest_option = None

            # Если аптека закрывается скоро, ищем альтернативу, которая не закрывается скоро
            if pharmacy_closes_soon:
                logger.info(f"Step 4: Pharmacy {source['code']} closes soon, looking for an alternative")
                # Ищем самую дешевую аптеку, которая не закрывается скоро
                if not alternative_cheapest_option:
                    for alt_option in delivery_data:
                        alt_pharmacy = alt_option["pharmacy"]
                        alt_source = alt_pharmacy.get("pharmacy", {}).get("source", {})
                        alt_closes_at = alt_source.get("closes_at")
                        alt_opening_hours = alt_source.get("opening_hours", "")

                        alt_pharmacy_closes_soon = is_pharmacy_open_soon(alt_closes_at, alt_opening_hours)
                        alt_pharmacy_closed = is_pharmacy_closed(alt_closes_at, alt_opening_hours)

                        # Логика для поиска самой дешевой альтернативы, которая не закрывается скоро
                        if not alt_pharmacy_closes_soon and not alt_pharmacy_closed and \
                                (alternative_cheapest_option is None or alt_option["total_price"] <
                                 alternative_cheapest_option["total_price"]):
                            logger.info(
                                f"Step 5: Found alternative_cheapest_option with code {alt_source['code']}, works longer than 1 hour, and price {alt_option['total_price']}")
                            alternative_cheapest_option = alt_option

        else:
            # Если аптека закрыта, проверяем, дешевле ли она на 30% по сравнению с самой дешевой открытой
            if cheapest_open_pharmacy and option["total_price"] <= cheapest_open_pharmacy["total_price"] * 0.7:
                logger.info(f"difference: option['total_price']: {option['total_price']} cheapest_open_pharmacy * 0.7 = {cheapest_open_pharmacy['total_price'] * 0.7}")
                logger.info(f"Step 6: Closed pharmacy {source['code']} is 30% cheaper than the open one. Setting as cheapest_closed_pharmacy")
                cheapest_closed_pharmacy = option

        # Логика для самой быстрой аптеки
        if not pharmacy_closed:
            # Сохраняем самую быструю открытую аптеку
            if fastest_open_pharmacy is None or option["delivery_option"]["eta"] < fastest_open_pharmacy["delivery_option"]["eta"]:
                logger.info(
                    f"Step 2.1: Setting fastest_open_pharmacy to {source['code']} with eta {option['delivery_option']['eta']}")
                fastest_open_pharmacy = option
                # Если аптека не закрывается скоро, не нужно альтернативы
                if not pharmacy_closes_soon:
                    logger.info(
                        f"Step 3.1: Pharmacy {source['code']} works longer than 1 hour, resetting alternative_fastest_option to None")
                    alternative_fastest_option = None

            # Если аптека закрывается скоро, ищем альтернативу, которая не закрывается скоро
            if pharmacy_closes_soon:
                logger.info(f"Step 4.1: Pharmacy {source['code']} closes soon, looking for an alternative fastest pharmacy")
                # Ищем самую быструю аптеку, которая не закрывается скоро
                if not alternative_fastest_option:
                    for alt_option in delivery_data:
                        alt_pharmacy = alt_option["pharmacy"]
                        alt_source = alt_pharmacy.get("pharmacy", {}).get("source", {})
                        alt_closes_at = alt_source.get("closes_at")
                        alt_opening_hours = alt_source.get("opening_hours", "")

                        alt_pharmacy_closes_soon = is_pharmacy_open_soon(alt_closes_at, alt_opening_hours)
                        alt_pharmacy_closed = is_pharmacy_closed(alt_closes_at, alt_opening_hours)

                        # Логика для поиска самой быстрой альтернативы, которая не закрывается скоро
                        if not alt_pharmacy_closes_soon and not alt_pharmacy_closed and \
                                (alternative_fastest_option is None or alt_option["delivery_option"]["eta"] <
                                 alternative_fastest_option["delivery_option"]["eta"]):
                            logger.info(
                                f"Step 5.1: Found alternative_fastest_option with code {alt_source['code']}, works longer than 1 hour, and eta {alt_option['delivery_option']['eta']}")
                            alternative_fastest_option = alt_option

        else:
            # Если аптека закрыта, проверяем, быстрее ли она на 30% по сравнению с самой быстрой открытой
            if fastest_open_pharmacy and option["delivery_option"]["eta"] <= fastest_open_pharmacy["delivery_option"]["eta"] * 0.7:
                logger.info(f"Step 6.1: Closed pharmacy {source['code']} is 30% faster than the open one. Setting as fastest_closed_pharmacy")
                fastest_closed_pharmacy = option

    # Если найдена закрытая аптека с 30% скидкой, возвращаем её вместе с самой дешевой открытой
    if cheapest_closed_pharmacy and cheapest_open_pharmacy:
        logger.info("Step 7: Returning both cheapest open and cheapest closed pharmacies due to 30% discount")
        return {
            "cheapest_delivery_option": cheapest_open_pharmacy,
            "alternative_cheapest_option": cheapest_closed_pharmacy,
            "fastest_delivery_option": fastest_open_pharmacy,
            "alternative_fastest_option": fastest_closed_pharmacy
        }

    # Возвращаем стандартные результаты
    logger.info(
        f"Step 8: Returning the standard results with cheapest_open_pharmacy: {cheapest_open_pharmacy['pharmacy']['pharmacy']['source']['code']}, fastest_open_pharmacy: {fastest_open_pharmacy['pharmacy']['pharmacy']['source']['code']}, alternative_cheapest_option: {alternative_cheapest_option}, alternative_fastest_option: {alternative_fastest_option}")
    return {
        "cheapest_delivery_option": cheapest_open_pharmacy,
        "alternative_cheapest_option": alternative_cheapest_option,
        "fastest_delivery_option": fastest_open_pharmacy,
        "alternative_fastest_option": alternative_fastest_option
    }



#  функция для проверки выбранных на каждой стадии отбора аптек (сохраняет списки аптек в файлы локально)
def save_response_to_file(data, file_name='data.json'):
    try:
        # Сохраняем данные в файл
        with open(file_name, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=4)

        print(f"Данные успешно сохранены в файл: {file_name}")
    except Exception as e:
        print(f"Ошибка при сохранении данных: {e}")


# мок ручки для возврата тестовых результатов запроса поиска аптек
@app.get("/search_medicines")
async def search_medicines():
    return JSONResponse(content={
        "result": [
            {
                "source": {
                    "code": "apteka_sadyhan_3mkr_20a",
                    "name": "Аптека 1",
                    "city": "Алматы",
                    "address": "Улица Брусиловского, 163",
                    "lat": 43.242913,
                    "lon": 76.877005,
                    "opening_hours": "Пн-Вс: 08:00-22:00",
                    "network_code": "apteka_chain_1",
                    "with_reserve": True,
                    "payment_on_site": True,
                    "kaspi_red": False,
                    "closes_at": "2024-10-21T17:00:00Z",
                    "opens_at": "2024-10-21T03:00:00Z",
                    "working_today": True,
                    "payment_by_card": True
                },
                "products": [
                    {
                        "source_code": "apteka_sadyhan_3mkr_20a",
                        "sku": "dospray_15ml",
                        "name": "Доспрей спрей назальный 15 мл",
                        "base_price": 1000,
                        "price_with_warehouse_discount": 750,
                        "warehouse_discount": 0,
                        "quantity": 1,
                        "quantity_desired": 1,
                        "diff": 0,
                        "avg_price": 0,
                        "min_price": 0,
                        "pp_packing": "1 шт.",
                        "manufacturer_id": "ЛеКос ТОО",
                        "recipe_needed": True,
                        "strong_recipe": False
                    },
                    {
                        "source_code": "apteka_sadyhan_3mkr_20a",
                        "sku": "viagra_100mg",
                        "name": "Виагра таблетки 100 мг №4",
                        "base_price": 0,
                        "price_with_warehouse_discount": 0,
                        "warehouse_discount": 0,
                        "quantity": 0,
                        "quantity_desired": 1,
                        "diff": 0,
                        "avg_price": 0,
                        "min_price": 0,
                        "pp_packing": "4 шт",
                        "manufacturer_id": "Фарева Амбуаз",
                        "recipe_needed": True,
                        "strong_recipe": False,
                        "analogs": [
                            {
                                "source_code": "apteka_sadyhan_3mkr_20a",
                                "sku": "kamagra_100mg",
                                "name": "Камагра 100 таблетки 100 мг №4",
                                "base_price": 2000,
                                "price_with_warehouse_discount": 5300,
                                "warehouse_discount": 0,
                                "quantity": 1,
                                "quantity_desired": 1,
                                "diff": 0,
                                "avg_price": 0,
                                "min_price": 0,
                                "pp_packing": "4 шт.",
                                "manufacturer_id": "Ajanta Pharma Ltd",
                                "recipe_needed": True,
                                "strong_recipe": False
                            }
                        ]
                    }
                ],
                "total_sum": 750,
                "avg_sum": 750,
                "min_sum": 750
            },
            {
                "source": {
                    "code": "apteka_sadyhan_5mkr_19b",
                    "name": "Аптека 2",
                    "city": "Алматы",
                    "address": "Проспект Абая, 115",
                    "lat": 43.239826,
                    "lon": 76.902216,
                    "opening_hours": "Пн-Вс: 09:00-00:00",
                    "network_code": "apteka_chain_2",
                    "with_reserve": True,
                    "payment_on_site": True,
                    "kaspi_red": False,
                    "closes_at": "2024-10-21T19:00:00Z",
                    "opens_at": "2024-10-21T04:00:00Z",
                    "working_today": True,
                    "payment_by_card": True
                },
                "products": [
                    {
                        "source_code": "apteka_sadyhan_5mkr_19b",
                        "sku": "dospray_15ml",
                        "name": "Доспрей спрей назальный 15 мл",
                        "base_price": 1000,
                        "price_with_warehouse_discount": 760,
                        "warehouse_discount": 0,
                        "quantity": 1,
                        "quantity_desired": 1,
                        "diff": 0,
                        "avg_price": 0,
                        "min_price": 0,
                        "pp_packing": "1 шт.",
                        "manufacturer_id": "ЛеКос ТОО",
                        "recipe_needed": True,
                        "strong_recipe": False
                    },
                    {
                        "source_code": "apteka_sadyhan_5mkr_19b",
                        "sku": "viagra_100mg",
                        "name": "Виагра таблетки 100 мг №4",
                        "base_price": 0,
                        "price_with_warehouse_discount": 0,
                        "warehouse_discount": 0,
                        "quantity": 0,
                        "quantity_desired": 1,
                        "diff": 0,
                        "avg_price": 0,
                        "min_price": 0,
                        "pp_packing": "4 шт",
                        "manufacturer_id": "Фарева Амбуаз",
                        "recipe_needed": True,
                        "strong_recipe": False,
                        "analogs": [
                            {
                                "source_code": "apteka_sadyhan_5mkr_19b",
                                "sku": "synagra_100mg",
                                "name": "Синегра таблетки 100 мг №4",
                                "base_price": 2000,
                                "price_with_warehouse_discount": 8000,
                                "warehouse_discount": 0,
                                "quantity": 1,
                                "quantity_desired": 1,
                                "diff": 0,
                                "avg_price": 0,
                                "min_price": 0,
                                "pp_packing": "4 шт.",
                                "manufacturer_id": "Ajanta Pharma Ltd",
                                "recipe_needed": True,
                                "strong_recipe": False
                            }
                        ]
                    }
                ],
                "total_sum": 760,
                "avg_sum": 760,
                "min_sum": 760
            },
            {
                "source": {
                    "code": "apteka_sadyhan_almaty_satpaeva_90_20",
                    "name": "Аптека 3",
                    "city": "Алматы",
                    "address": "Улица Макатаева, 53",
                    "lat": 43.264685,
                    "lon": 76.950991,
                    "opening_hours": "Пн-Вс: 09:00-00:00",
                    "network_code": "apteka_chain_3",
                    "with_reserve": False,
                    "payment_on_site": False,
                    "kaspi_red": False,
                    "closes_at": "2024-10-21T19:00:00Z",
                    "opens_at": "2024-10-21T04:00:00Z",
                    "working_today": True,
                    "payment_by_card": False
                },
                "products": [
                    {
                        "source_code": "apteka_sadyhan_almaty_satpaeva_90_20",
                        "sku": "dospray_15ml",
                        "name": "Доспрей спрей назальный 15 мл",
                        "base_price": 1000,
                        "price_with_warehouse_discount": 775,
                        "warehouse_discount": 0,
                        "quantity": 1,
                        "quantity_desired": 1,
                        "diff": 0,
                        "avg_price": 0,
                        "min_price": 0,
                        "pp_packing": "1 шт.",
                        "manufacturer_id": "ЛеКос ТОО",
                        "recipe_needed": True,
                        "strong_recipe": False
                    },
                    {
                        "source_code": "apteka_sadyhan_almaty_satpaeva_90_20",
                        "sku": "viagra_100mg",
                        "name": "Виагра таблетки 100 мг №4",
                        "base_price": 0,
                        "price_with_warehouse_discount": 0,
                        "warehouse_discount": 0,
                        "quantity": 0,
                        "quantity_desired": 1,
                        "diff": 0,
                        "avg_price": 0,
                        "min_price": 0,
                        "pp_packing": "4 шт",
                        "manufacturer_id": "Фарева Амбуаз",
                        "recipe_needed": True,
                        "strong_recipe": False,
                        "analogs": [
                            {
                                "source_code": "apteka_sadyhan_almaty_satpaeva_90_20",
                                "sku": "silfect_100mg",
                                "name": "Силфект таблетки 100 мг №4",
                                "base_price": 3000,
                                "price_with_warehouse_discount": 4700,
                                "warehouse_discount": 0,
                                "quantity": 1,
                                "quantity_desired": 1,
                                "diff": 0,
                                "avg_price": 0,
                                "min_price": 0,
                                "pp_packing": "4 шт.",
                                "manufacturer_id": "Уорлд Медицин Илач Сан.ве Тидж",
                                "recipe_needed": True,
                                "strong_recipe": False
                            }
                        ]
                    }
                ],
                "total_sum": 775,
                "avg_sum": 775,
                "min_sum": 775
            },
            # {
            #     "source": {
            #         "code": "apteka_so_sklada_timiryazeva_44",
            #         "name": "Аптека 4",
            #         "city": "Алматы",
            #         "address": "Проспект Райымбека, 200",
            #         "lat": 43.284726,
            #         "lon": 76.945817,
            #         "opening_hours": "Пн-Вс: 08:00-23:00",
            #         "network_code": "apteka_chain_4",
            #         "with_reserve": False,
            #         "payment_on_site": True,
            #         "kaspi_red": False,
            #         "closes_at": "2024-10-21T18:00:00Z",
            #         "opens_at": "2024-10-21T03:00:00Z",
            #         "working_today": True,
            #         "payment_by_card": True
            #     },
            #     "products": [
            #         {
            #             "source_code": "apteka_so_sklada_timiryazeva_44",
            #             "sku": "dospray_15ml",
            #             "name": "Доспрей спрей назальный 15 мл",
            #             "base_price": 740,
            #             "price_with_warehouse_discount": 740,
            #             "warehouse_discount": 0,
            #             "quantity": 1,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "1 шт.",
            #             "manufacturer_id": "ЛеКос ТОО",
            #             "recipe_needed": True,
            #             "strong_recipe": False
            #         },
            #         {
            #             "source_code": "apteka_so_sklada_timiryazeva_44",
            #             "sku": "viagra_100mg",
            #             "name": "Виагра таблетки 100 мг №4",
            #             "base_price": 0,
            #             "price_with_warehouse_discount": 0,
            #             "warehouse_discount": 0,
            #             "quantity": 0,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "4 шт",
            #             "manufacturer_id": "Фарева Амбуаз",
            #             "recipe_needed": True,
            #             "strong_recipe": False,
            #             "analogs": [
            #                 {
            #                     "source_code": "apteka_so_sklada_timiryazeva_44",
            #                     "sku": "kamagra_100mg",
            #                     "name": "Камагра таблетки 100 мг №4",
            #                     "base_price": 5400,
            #                     "price_with_warehouse_discount": 5400,
            #                     "warehouse_discount": 0,
            #                     "quantity": 1,
            #                     "quantity_desired": 1,
            #                     "diff": 0,
            #                     "avg_price": 0,
            #                     "min_price": 0,
            #                     "pp_packing": "4 шт.",
            #                     "manufacturer_id": "Ajanta Pharma Ltd",
            #                     "recipe_needed": True,
            #                     "strong_recipe": False
            #                 }
            #             ]
            #         }
            #     ],
            #     "total_sum": 740,
            #     "avg_sum": 740,
            #     "min_sum": 740
            # },
            # {
            #     "source": {
            #         "code": "apteka_sadyhan_nazarbaeva_240",
            #         "name": "Аптека 5",
            #         "city": "Алматы",
            #         "address": "Проспект Абылайхана, 120",
            #         "lat": 43.268123,
            #         "lon": 76.920123,
            #         "opening_hours": "Пн-Вс: 09:00-21:00",
            #         "network_code": "apteka_chain_5",
            #         "with_reserve": False,
            #         "payment_on_site": False,
            #         "kaspi_red": False,
            #         "closes_at": "2024-10-21T16:00:00Z",
            #         "opens_at": "2024-10-21T03:00:00Z",
            #         "working_today": True,
            #         "payment_by_card": False
            #     },
            #     "products": [
            #         {
            #             "source_code": "apteka_sadyhan_nazarbaeva_240",
            #             "sku": "dospray_15ml",
            #             "name": "Доспрей спрей назальный 15 мл",
            #             "base_price": 770,
            #             "price_with_warehouse_discount": 770,
            #             "warehouse_discount": 0,
            #             "quantity": 1,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "1 шт.",
            #             "manufacturer_id": "ЛеКос ТОО",
            #             "recipe_needed": True,
            #             "strong_recipe": False
            #         },
            #         {
            #             "source_code": "apteka_sadyhan_nazarbaeva_240",
            #             "sku": "viagra_100mg",
            #             "name": "Виагра таблетки 100 мг №4",
            #             "base_price": 0,
            #             "price_with_warehouse_discount": 0,
            #             "warehouse_discount": 0,
            #             "quantity": 0,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "4 шт",
            #             "manufacturer_id": "Фарева Амбуаз",
            #             "recipe_needed": True,
            #             "strong_recipe": False,
            #             "analogs": [
            #                 {
            #                     "source_code": "apteka_sadyhan_nazarbaeva_240",
            #                     "sku": "kamagra_100mg",
            #                     "name": "Камагра таблетки 100 мг №4",
            #                     "base_price": 5200,
            #                     "price_with_warehouse_discount": 5200,
            #                     "warehouse_discount": 0,
            #                     "quantity": 1,
            #                     "quantity_desired": 1,
            #                     "diff": 0,
            #                     "avg_price": 0,
            #                     "min_price": 0,
            #                     "pp_packing": "4 шт.",
            #                     "manufacturer_id": "Ajanta Pharma Ltd",
            #                     "recipe_needed": True,
            #                     "strong_recipe": False
            #                 }
            #             ]
            #         }
            #     ],
            #     "total_sum": 770,
            #     "avg_sum": 770,
            #     "min_sum": 770
            # },
            # {
            #     "source": {
            #         "code": "apteka_sadyhan_dostyk_91_2",
            #         "name": "Аптека 6",
            #         "city": "Алматы",
            #         "address": "Улица Гагарина, 210",
            #         "lat": 43.265831,
            #         "lon": 76.929713,
            #         "opening_hours": "Пн-Вс: 09:00-23:00",
            #         "network_code": "apteka_chain_6",
            #         "with_reserve": True,
            #         "payment_on_site": True,
            #         "kaspi_red": False,
            #         "closes_at": "2024-10-21T18:00:00Z",
            #         "opens_at": "2024-10-21T03:00:00Z",
            #         "working_today": True,
            #         "payment_by_card": False
            #     },
            #     "products": [
            #         {
            #             "source_code": "apteka_sadyhan_dostyk_91_2",
            #             "sku": "dospray_15ml",
            #             "name": "Доспрей спрей назальный 15 мл",
            #             "base_price": 790,
            #             "price_with_warehouse_discount": 790,
            #             "warehouse_discount": 0,
            #             "quantity": 1,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "1 шт.",
            #             "manufacturer_id": "ЛеКос ТОО",
            #             "recipe_needed": True,
            #             "strong_recipe": False
            #         },
            #         {
            #             "source_code": "apteka_sadyhan_dostyk_91_2",
            #             "sku": "viagra_100mg",
            #             "name": "Виагра таблетки 100 мг №4",
            #             "base_price": 0,
            #             "price_with_warehouse_discount": 0,
            #             "warehouse_discount": 0,
            #             "quantity": 0,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "4 шт",
            #             "manufacturer_id": "Фарева Амбуаз",
            #             "recipe_needed": True,
            #             "strong_recipe": False,
            #             "analogs": [
            #                 {
            #                     "source_code": "apteka_sadyhan_dostyk_91_2",
            #                     "sku": "synagra_100mg",
            #                     "name": "Синегра таблетки 100 мг №4",
            #                     "base_price": 8300,
            #                     "price_with_warehouse_discount": 8300,
            #                     "warehouse_discount": 0,
            #                     "quantity": 1,
            #                     "quantity_desired": 1,
            #                     "diff": 0,
            #                     "avg_price": 0,
            #                     "min_price": 0,
            #                     "pp_packing": "4 шт.",
            #                     "manufacturer_id": "Ajanta Pharma Ltd",
            #                     "recipe_needed": True,
            #                     "strong_recipe": False
            #                 }
            #             ]
            #         }
            #     ],
            #     "total_sum": 790,
            #     "avg_sum": 790,
            #     "min_sum": 790
            # },
            # {
            #     "source": {
            #         "code": "apteka_sadyhan_almaty_demchenko_89",
            #         "name": "Аптека 7",
            #         "city": "Алматы",
            #         "address": "Проспект Назарбаева, 99",
            #         "lat": 43.270942,
            #         "lon": 76.920817,
            #         "opening_hours": "Пн-Вс: 08:00-22:00",
            #         "network_code": "apteka_chain_7",
            #         "with_reserve": True,
            #         "payment_on_site": True,
            #         "kaspi_red": False,
            #         "closes_at": "2024-10-21T17:00:00Z",
            #         "opens_at": "2024-10-21T03:00:00Z",
            #         "working_today": True,
            #         "payment_by_card": True
            #     },
            #     "products": [
            #         {
            #             "source_code": "apteka_sadyhan_almaty_demchenko_89",
            #             "sku": "dospray_15ml",
            #             "name": "Доспрей спрей назальный 15 мл",
            #             "base_price": 745,
            #             "price_with_warehouse_discount": 745,
            #             "warehouse_discount": 0,
            #             "quantity": 1,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "1 шт.",
            #             "manufacturer_id": "ЛеКос ТОО",
            #             "recipe_needed": True,
            #             "strong_recipe": False
            #         },
            #         {
            #             "source_code": "apteka_sadyhan_almaty_demchenko_89",
            #             "sku": "viagra_100mg",
            #             "name": "Виагра таблетки 100 мг №4",
            #             "base_price": 0,
            #             "price_with_warehouse_discount": 0,
            #             "warehouse_discount": 0,
            #             "quantity": 0,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "4 шт",
            #             "manufacturer_id": "Фарева Амбуаз",
            #             "recipe_needed": True,
            #             "strong_recipe": False,
            #             "analogs": [
            #                 {
            #                     "source_code": "apteka_sadyhan_almaty_demchenko_89",
            #                     "sku": "kamagra_100mg",
            #                     "name": "Камагра таблетки 100 мг №4",
            #                     "base_price": 5100,
            #                     "price_with_warehouse_discount": 5100,
            #                     "warehouse_discount": 0,
            #                     "quantity": 1,
            #                     "quantity_desired": 1,
            #                     "diff": 0,
            #                     "avg_price": 0,
            #                     "min_price": 0,
            #                     "pp_packing": "4 шт.",
            #                     "manufacturer_id": "Ajanta Pharma Ltd",
            #                     "recipe_needed": True,
            #                     "strong_recipe": False
            #                 }
            #             ]
            #         }
            #     ],
            #     "total_sum": 745,
            #     "avg_sum": 745,
            #     "min_sum": 745
            # },
            # {
            #     "source": {
            #         "code": "apteka_sadyhan_talgar",
            #         "name": "Аптека 8",
            #         "city": "Алматы",
            #         "address": "Проспект Тауельсиздик, 100",
            #         "lat": 43.285741,
            #         "lon": 76.902374,
            #         "opening_hours": "Круглосуточно",
            #         "network_code": "apteka_chain_8",
            #         "with_reserve": False,
            #         "payment_on_site": True,
            #         "kaspi_red": False,
            #         "closes_at": "2024-10-21T16:00:00Z",
            #         "opens_at": "2024-10-21T04:00:00Z",
            #         "working_today": True,
            #         "payment_by_card": True
            #     },
            #     "products": [
            #         {
            #             "source_code": "apteka_sadyhan_talgar",
            #             "sku": "dospray_15ml",
            #             "name": "Доспрей спрей назальный 15 мл",
            #             "base_price": 765,
            #             "price_with_warehouse_discount": 765,
            #             "warehouse_discount": 0,
            #             "quantity": 1,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "1 шт.",
            #             "manufacturer_id": "ЛеКос ТОО",
            #             "recipe_needed": True,
            #             "strong_recipe": False
            #         },
            #         {
            #             "source_code": "apteka_sadyhan_talgar",
            #             "sku": "viagra_100mg",
            #             "name": "Виагра таблетки 100 мг №4",
            #             "base_price": 0,
            #             "price_with_warehouse_discount": 0,
            #             "warehouse_discount": 0,
            #             "quantity": 0,
            #             "quantity_desired": 1,
            #             "diff": 0,
            #             "avg_price": 0,
            #             "min_price": 0,
            #             "pp_packing": "4 шт",
            #             "manufacturer_id": "Фарева Амбуаз",
            #             "recipe_needed": True,
            #             "strong_recipe": False,
            #             "analogs": [
            #                 {
            #                     "source_code": "apteka_sadyhan_talgar",
            #                     "sku": "synagra_100mg",
            #                     "name": "Синегра таблетки 100 мг №4",
            #                     "base_price": 8200,
            #                     "price_with_warehouse_discount": 8200,
            #                     "warehouse_discount": 0,
            #                     "quantity": 1,
            #                     "quantity_desired": 1,
            #                     "diff": 0,
            #                     "avg_price": 0,
            #                     "min_price": 0,
            #                     "pp_packing": "4 шт.",
            #                     "manufacturer_id": "Ajanta Pharma Ltd",
            #                     "recipe_needed": True,
            #                     "strong_recipe": False
            #                 }
            #             ]
            #         }
            #     ],
            #     "total_sum": 765,
            #     "avg_sum": 765,
            #     "min_sum": 765
            # }
        ]
    })
