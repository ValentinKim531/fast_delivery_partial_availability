import os
from collections import defaultdict

import httpx
import math
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging
from fastapi.middleware.cors import CORSMiddleware
import json
from dotenv import load_dotenv
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

    try:
        request_data = await request.json()
        encoded_city = request_data.get("city")
        sku_data = request_data.get("skus", [])
        address = request_data.get("address", {})

        user_lat = request_data.get("address", {}).get("lat")
        user_lon = request_data.get("address", {}).get("lng")

        if not encoded_city or not sku_data or user_lat is None or user_lon is None:
            return JSONResponse(content={"error": "City, SKU data, and user coordinates are required"}, status_code=400)

        if not isinstance(user_lat, (int, float)) or not isinstance(user_lon, (int, float)):
            return JSONResponse(content={"error": "Invalid data type for user coordinates"}, status_code=400)

        for item in sku_data:
            if not isinstance(item.get("sku"), str) or not isinstance(item.get("count_desired"), int):
                return JSONResponse(content={"error": "Invalid SKU format or count type"}, status_code=400)


        payload = [{"sku": item["sku"], "count_desired": item["count_desired"]} for item in sku_data]

        # Поиск лекарств в аптеках
        pharmacies = await find_medicines_in_pharmacies(encoded_city, payload)
        # Проверка, если результат поиска пуст
        if not pharmacies.get("result"):
            logger.error("No pharmacies found with the provided SKU data")
            return JSONResponse(content={"error": "No pharmacies found with the provided SKU data"}, status_code=500)
        save_response_to_file(pharmacies, file_name='data1_found_all.json')

        pharmacies_with_missing_items = await filter_pharmacies_with_missing_items(pharmacies, sku_data)
        save_response_to_file(pharmacies_with_missing_items, file_name='data1_2_found_all__with_missing_items.json')

        # Поиск аптек с учетом наличия приоритетного товара
        filtered_pharmacies = await filter_pharmacies_by_priority_items(pharmacies_with_missing_items, sku_data)
        if isinstance(filtered_pharmacies, JSONResponse):
            return filtered_pharmacies
        save_response_to_file(filtered_pharmacies, file_name='data2_found_with_priority.json')

        # Сортировка по наибольшему количеству доступных товаров
        top_pharmacies = await sort_pharmacies_by_fulfillment(filtered_pharmacies)
        save_response_to_file(top_pharmacies, file_name='data3_sorted_pharmacies.json')


        # Выбор ближайших и самых дешевых аптек
        closest_pharmacies = await get_top_closest_pharmacies(top_pharmacies, user_lat, user_lon)
        save_response_to_file(closest_pharmacies, file_name='data4_top_closest_pharmacies.json')

        cheapest_pharmacies = await get_top_cheapest_pharmacies(top_pharmacies)
        save_response_to_file(cheapest_pharmacies, file_name='data4_top_cheapest_pharmacies.json')


        # Расчет вариантов доставки
        delivery_options1 = await get_delivery_options(closest_pharmacies, user_lat, user_lon)
        if isinstance(delivery_options1, JSONResponse):
            return delivery_options1  # Возвращаем JSONResponse сразу, если это ошибка
        save_response_to_file(delivery_options1, file_name='data5_delivery_options_closest.json')

        delivery_options2 = await get_delivery_options(cheapest_pharmacies, user_lat, user_lon)
        if isinstance(delivery_options2, JSONResponse):
            return delivery_options1  # Возвращаем JSONResponse сразу, если это ошибка
        save_response_to_file(delivery_options2, file_name='data5_delivery_options_cheapest.json')

        all_delivery_options = delivery_options1 + delivery_options2
        save_response_to_file(all_delivery_options, file_name='data5_all_delivery_options.json')

        result = await best_option(all_delivery_options)
        save_response_to_file(result, file_name='data6_final_result.json')
        return result

    except json.JSONDecodeError:
        return JSONResponse(content={"error": "Invalid JSON format"}, status_code=400)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return JSONResponse(content={"error": "An unexpected error occurred"}, status_code=500)


async def find_medicines_in_pharmacies(encoded_city, payload):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(URL_SEARCH, params=encoded_city, json=payload)
            response.raise_for_status()
            data = response.json()
            # Проверка корректности данных от API
            if not isinstance(data, dict) or "result" not in data:
                return JSONResponse(content={"error": "Invalid response format from search API"}, status_code=502)
            return data
        except httpx.RequestError as e:
            logger.error(f"Request error while accessing URL_SEARCH: {e}")
            return JSONResponse(content={"error": "Request error while accessing search API"}, status_code=503)
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while accessing URL_SEARCH: {e}")
            return JSONResponse(content={"error": f"HTTP error {e.response.status_code}"}, status_code=e.response.status_code)


# мок для тестирования локальных результатов поиска
# async def find_medicines_in_pharmacies(encoded_city, payload):
#     async with httpx.AsyncClient() as client:
#         response = await client.get("http://localhost:8003/search_medicines")
#         response.raise_for_status()  # Проверка на ошибки
#         data = response.json()  # Получаем JSON
#         return data  # Возвращаем JSON данные



async def filter_pharmacies_with_missing_items(pharmacies, priority_skus):
    pharmacies_with_missing_items = []

    for pharmacy in pharmacies.get("result", []):
        products = pharmacy.get("products", [])
        has_missing_item = False  # Флаг для проверки отсутствия хотя бы одного товара

        for priority_sku in priority_skus:
            item_found = False  # Отслеживаем, найден ли товар в нужном количестве

            # Проверка наличия основного товара
            for product in products:
                if product["sku"] == priority_sku["sku"]:
                    if product["quantity"] >= priority_sku["count_desired"]:
                        # Основной товар найден в нужном количестве
                        item_found = True
                    else:
                        # Основного товара недостаточно, проверяем аналоги
                        available_analogs = [
                            analog for analog in product.get("analogs", [])
                            if analog["quantity"] >= priority_sku["count_desired"]
                        ]
                        if available_analogs:
                            # Есть аналоги в нужном количестве
                            item_found = True

                    # Прекращаем проверку по текущему товару, если он найден в нужном количестве
                    break

            # Если ни основной товар, ни его аналоги не найдены в достаточном количестве
            if not item_found:
                has_missing_item = True
                break

        # Добавляем аптеку в результат, если хотя бы одного товара не хватает
        if has_missing_item:
            pharmacies_with_missing_items.append(pharmacy)

    return {"result": pharmacies_with_missing_items}



# Фильтр аптек с учетом приоритетности товаров от первого в списке запроса и далее
async def filter_pharmacies_by_priority_items(pharmacies, priority_skus):
    """
    Функция для последовательного фильтрации аптек по приоритетным товарам с учетом аналогов.
    """
    if "result" not in pharmacies or not isinstance(pharmacies["result"], list):
        logger.error("Invalid pharmacies data format.")
        return JSONResponse(content={"error": "Invalid pharmacies data format"}, status_code=502)

    filtered_pharmacies = pharmacies.get("result", [])
    logger.info(f"Initial pharmacies count: {len(filtered_pharmacies)}")
    found_any_product = False  # Флаг для проверки наличия хотя бы одного товара в аптеках

    for round_number, priority_sku in enumerate(priority_skus, start=1):
        logger.info(f"Processing priority SKU {round_number}/{len(priority_skus)}: {priority_sku}")
        temp_filtered_pharmacies = []

        for pharmacy in filtered_pharmacies:
            logger.info(f"Checking pharmacy: {pharmacy.get('source', {}).get('name', 'Unknown')}")
            products = pharmacy.get("products", [])
            updated_products = products[:]
            replacements_needed = 0
            replaced_skus = []
            product_found = False

            for product in updated_products:
                if product["sku"] == priority_sku["sku"]:
                    if product["quantity"] >= priority_sku["count_desired"]:
                        # Если оригинал найден и достаточно, добавляем его
                        logger.info(f"Product {product['sku']} has sufficient quantity")
                        product["quantity_desired"] = priority_sku["count_desired"]
                        product_found = True
                        found_any_product = True
                        break
                    else:
                        # Если недостаточно, проверяем аналоги
                        logger.info(f"Insufficient quantity for SKU: {product['sku']}, checking analogs")
                        cheapest_analog = min(
                            product.get("analogs", []),
                            key=lambda analog: analog["base_price"],
                            default=None
                        )
                        if cheapest_analog and cheapest_analog["quantity"] >= priority_sku["count_desired"]:

                            product["quantity_desired"] = priority_sku["count_desired"]
                            product["analogs"] = [{
                                "source_code": cheapest_analog["source_code"],
                                "sku": cheapest_analog["sku"],
                                "name": cheapest_analog["name"],
                                "base_price": cheapest_analog["base_price"],
                                "price_with_warehouse_discount": cheapest_analog["price_with_warehouse_discount"],
                                "warehouse_discount": cheapest_analog["warehouse_discount"],
                                "quantity": cheapest_analog["quantity"],
                                "quantity_desired": priority_sku["count_desired"],
                                "diff": product["diff"],
                                "avg_price": product["avg_price"],
                                "min_price": product["min_price"],
                                "pp_packing": cheapest_analog.get("pp_packing", ""),
                                "manufacturer_id": cheapest_analog.get("manufacturer_id", ""),
                                "recipe_needed": cheapest_analog["recipe_needed"],
                                "strong_recipe": cheapest_analog["strong_recipe"],
                            }]
                            replacements_needed += 1
                            replaced_skus.append({
                                "original_sku": product["sku"],
                                "replacement_sku": cheapest_analog["sku"]
                            })
                            product_found = True
                            found_any_product = True
                            logger.info(f"replaced_skus1: {replaced_skus}")
                            break

            # Если ни оригинала, ни аналога не хватает, удаляем продукт только для текущего priority_sku
            if not product_found:
                updated_products = [p for p in updated_products if p["sku"] != priority_sku["sku"]]
                logger.info(f"Removing product SKU: {priority_sku['sku']} from pharmacy due to insufficient stock")

            # Проверка финального списка продуктов в аптеке после всех удалений и замен
            logger.info(f"Final product list for pharmacy after SKU '{priority_sku['sku']}': {[p['sku'] for p in updated_products]}")

            logger.info(f"replaced_skus2: {replaced_skus}")

            # Сохраняем аптеку только если продукт найден (оригинал или аналог) или это не последний SKU
            if product_found:
                temp_filtered_pharmacies.append({
                    "source": pharmacy["source"],
                    "products": updated_products,
                    "replacements_needed": replacements_needed,
                    "replaced_skus": replaced_skus
                })

        # Обновляем список аптек для следующего SKU
        if temp_filtered_pharmacies:
            filtered_pharmacies = temp_filtered_pharmacies
            logger.info(f"Filtered pharmacies count after SKU '{priority_sku['sku']}': {len(filtered_pharmacies)}")
        elif not found_any_product:
            logger.info(f"No pharmacies found after filtering for SKU '{priority_sku['sku']}'")
            continue
            # return JSONResponse(content={
            #     "error": "No pharmacies found meeting the SKU requirements or available quantities."
            # }, status_code=500)

        # Сохраняем промежуточный результат для каждого круга
        save_response_to_file({"filtered_pharmacies": filtered_pharmacies},
                              file_name=f'data_round_{round_number}_filtered_pharmacies.json')

    # Финальный подсчет total_sum после всех раундов
    for pharmacy in filtered_pharmacies:
        pharmacy["total_sum"] = sum(
            # Если у продукта есть аналог с достаточным количеством, используем его для подсчета суммы
            (product["analogs"][0]["base_price"] * product["analogs"][0]["quantity"]
             if product.get("analogs") and product["analogs"][0]["quantity"] >= product["quantity_desired"]
             # Иначе считаем только основной продукт, если его количество соответствует желаемому
             else product["base_price"] * product["quantity"])
            for product in pharmacy["products"]
            if "quantity_desired" in product
        )

    # Итоговое сохранение после всех кругов обработки
    save_response_to_file({"filtered_pharmacies": filtered_pharmacies}, file_name="final_filtered_pharmacies.json")
    return {"filtered_pharmacies": filtered_pharmacies}










# Сортировка аптек по количеству доступных товаров и выбор аптек с наибольшей корзиной
async def sort_pharmacies_by_fulfillment(pharmacies_with_partial_availability):
    # Группируем аптеки по количеству доступных товаров в корзине
    grouped_pharmacies = defaultdict(list)

    for pharmacy in pharmacies_with_partial_availability.get("filtered_pharmacies", []):
        # Получаем количество товаров в корзине для каждой аптеки
        num_products = len(pharmacy.get("products", []))
        grouped_pharmacies[num_products].append(pharmacy)

    # Находим максимальное количество товаров в корзине
    max_products = max(grouped_pharmacies.keys(), default=0)

    # Берем все аптеки, у которых это максимальное количество товаров
    top_pharmacies = grouped_pharmacies[max_products]

    # Логируем количество аптек и товаров
    logger.info(f"Выбрано {len(top_pharmacies)} аптек с максимальной корзиной из {max_products} товаров")

    return {"filtered_pharmacies": top_pharmacies}




# Функция для выбора ближайших 2 аптек
async def get_top_closest_pharmacies(pharmacies, user_lat, user_lon):
    pharmacies_with_distance = []
    for pharmacy in pharmacies.get("filtered_pharmacies", []):
        source_info = pharmacy.get("source", {})
        pharmacy_lat = source_info.get("lat")
        pharmacy_lon = source_info.get("lon")

        # Проверяем если lat/lon существует, перед расчетом дистанции
        if pharmacy_lat is None or pharmacy_lon is None:
            continue  # пропускаем если lat/lon отсутствуют

        distance = haversine_distance(user_lat, user_lon, pharmacy_lat, pharmacy_lon)
        pharmacies_with_distance.append({"pharmacy": pharmacy, "distance": distance})

    # сортируем аптеки по дистанции от самой близкой и дальше
    sorted_pharmacies = sorted(pharmacies_with_distance, key=lambda x: x["distance"])

    # получаем ТОП 2 ближайшие аптеки
    closest_pharmacies = [item["pharmacy"] for item in sorted_pharmacies[:2]]

    return {"list_pharmacies": closest_pharmacies}


# Функция для выбора самых дешевых 3 аптек
async def get_top_cheapest_pharmacies(pharmacies):
    sorted_pharmacies = sorted(
        pharmacies.get("filtered_pharmacies", []),
        key=lambda x: x["pharmacy"].get("total_sum", float('inf')) if "pharmacy" in x else float('inf')
    )

    return {"list_pharmacies": sorted_pharmacies[:3]}


# Алгоритм расчета расстояния
def haversine_distance(lat1, lon1, lat2, lon2):
    return math.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2)


def is_pharmacy_open_soon(closes_at, opens_at, opening_hours):
    """Проверяет, закроется ли аптека через 1 час или если аптека работает круглосуточно."""
    almaty_tz = pytz.timezone('Asia/Almaty')
    current_time = datetime.now(almaty_tz)

    # Мок для тестов (замените на текущую дату при работе в продакшн)
    # current_time = almaty_tz.localize(datetime(2024, 10, 21, 22, 30, 0))

    # Проверка для круглосуточных аптек
    if opening_hours == "Круглосуточно":
        return False  # Круглосуточная аптека не закроется скоро

    try:
        # Конвертация времени открытия и закрытия в текущий часовой пояс
        closes_time = datetime.strptime(closes_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(almaty_tz)
        opens_time = datetime.strptime(opens_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(almaty_tz)
    except ValueError as e:
        logger.error(f"Time opens\closes parsing error: {e}")
        return True  # Если ошибка, считаем, что аптека закрыта для избежания ошибок


    # Проверяем, если аптека еще не открылась
    if current_time < opens_time:
        return False  # Если аптека еще не открылась, она не закроется скоро

    # Проверка, закроется ли аптека через 1 час или меньше
    return timedelta(0) <= closes_time - current_time <= timedelta(hours=1)


def is_pharmacy_closed(closes_at, opens_at, opening_hours):
    """Проверяет, закрыта ли аптека на момент запроса, учитывая расписание."""
    almaty_tz = pytz.timezone('Asia/Almaty')
    current_time = datetime.now(almaty_tz)

    # Мок для тестов (замените на текущую дату при работе в продакшн)
    # current_time = almaty_tz.localize(datetime(2024, 10, 21, 22, 30, 0))

    # Проверка, если аптека круглосуточная
    if opening_hours == "Круглосуточно":
        return False

    try:
        # Конвертация времени открытия и закрытия
        closes_time = datetime.strptime(closes_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(almaty_tz)
        opens_time = datetime.strptime(opens_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(almaty_tz)
    except ValueError as e:
        logger.error(f"Time opens\closes parsing error: {e}")
        return True  # Если ошибка, считаем, что аптека закрыта для избежания ошибок

    # Проверка если аптека закрыта сейчас и еще не открылась
    if current_time < opens_time:
        return True

    # Проверка если аптека уже закрылась, но еще не наступило новое время открытия
    if current_time >= closes_time and current_time < (opens_time + timedelta(days=1)):
        return True

    # Если текущее время находится в пределах открытия и закрытия
    return not (opens_time <= current_time < closes_time)


async def get_delivery_options(pharmacies, user_lat, user_lon):
    """Функция возвращает все данные о доставке для аптек без принятия решений."""

    # Проверка на наличие аптек
    if not pharmacies.get("list_pharmacies"):
        return JSONResponse(content={"error": "No pharmacies available for delivery options"}, status_code=404)

    results = []

    for pharmacy in pharmacies["list_pharmacies"]:
        source = pharmacy.get("source", {})
        products = pharmacy.get("products", [])

        if "code" not in source:
            continue

        pharmacy_total_sum = pharmacy.get("total_sum", 0)

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

                if delivery_data.get("status") == "success":
                    delivery_options = delivery_data["result"]["delivery"]

                    for option in delivery_options:
                        results.append({
                            "pharmacy": pharmacy,
                            "total_price": pharmacy_total_sum + option["price"],
                            "delivery_option": option
                        })
                else:
                    logger.error(f"Unexpected response format from URL_PRICE API: {delivery_data}")
                    return JSONResponse(
                        content={"error": "Unexpected response format from URL_PRICE API", "details": delivery_data},
                        status_code=502
                    )

            except httpx.RequestError as e:
                logger.error(f"Request error while accessing URL_PRICE: {e}")
                return JSONResponse(content={"error": "Request error while accessing URL_PRICE", "details": str(e)},
                                    status_code=502)

            except httpx.HTTPStatusError as e:
                error_details = e.response.json() if e.response.content else {"error": str(e)}
                logger.error(f"HTTP error while accessing URL_PRICE: {e}")
                return JSONResponse(
                    content={
                        "error": f"HTTP error {e.response.status_code}",
                        "details": error_details
                    },
                    status_code=e.response.status_code
                )

    return results


async def best_option(delivery_data):
    """Функция для сравнения аптек и выбора лучших опций с учетом времени закрытия, цены и условий."""

    # Проверка наличия данных о доставке
    if not delivery_data:
        return JSONResponse(content={"error": "No delivery options found"}, status_code=404)

    # Проверка корректности формата данных
    for option in delivery_data:
        if "pharmacy" not in option or "total_price" not in option or "delivery_option" not in option:
            return JSONResponse(content={"error": "Invalid delivery option data format"}, status_code=502)

    cheapest_open_pharmacy = None
    cheapest_closed_pharmacy = None
    alternative_cheapest_option = None

    fastest_open_pharmacy = None
    fastest_closed_pharmacy = None
    alternative_fastest_option = None

    # Первый проход для выбора самой дешевой и самой быстрой открытых аптек
    for option in delivery_data:
        pharmacy = option.get("pharmacy", {})
        source = pharmacy.get("source", {})
        closes_at = source.get("closes_at")
        opens_at = source.get("opens_at")
        opening_hours = source.get("opening_hours", "")

        if 'code' not in source:
            logger.warning(f"Missing 'code' in pharmacy source: {source}")
            continue

        pharmacy_closed = is_pharmacy_closed(closes_at, opens_at, opening_hours)
        pharmacy_closes_soon = is_pharmacy_open_soon(closes_at, opens_at, opening_hours) if closes_at else False

        if not pharmacy_closed:
            # Самая дешевая открытая аптека
            if cheapest_open_pharmacy is None or option["total_price"] < cheapest_open_pharmacy["total_price"]:
                cheapest_open_pharmacy = option
                if not pharmacy_closes_soon:
                    alternative_cheapest_option = None
                else:
                    logger.info(f"Step 4: Pharmacy {source['code']} closes soon, looking for an alternative")
                    # Ищем самую дешевую аптеку, которая не закрывается скоро
                    if not alternative_cheapest_option:
                        for alt_option in delivery_data:
                            alt_pharmacy = alt_option.get("pharmacy", {})
                            alt_source = alt_pharmacy.get("source", {})
                            alt_closes_at = alt_source.get("closes_at")
                            alt_opens_at = alt_source.get("opens_at")
                            alt_opening_hours = alt_source.get("opening_hours", "")

                            alt_pharmacy_closes_soon = is_pharmacy_open_soon(alt_closes_at, alt_opens_at, alt_opening_hours)
                            alt_pharmacy_closed = is_pharmacy_closed(alt_closes_at, alt_opens_at, alt_opening_hours)

                            # Логика для поиска самой дешевой альтернативы, которая не закрывается скоро
                            if not alt_pharmacy_closes_soon and not alt_pharmacy_closed and \
                                    (alternative_cheapest_option is None or alt_option["total_price"] <
                                     alternative_cheapest_option["total_price"]):
                                logger.info(
                                    f"Step 5: Found alternative_cheapest_option with code {alt_source.get('code')}, works longer than 1 hour, and price {alt_option['total_price']}")
                                alternative_cheapest_option = alt_option

            # Самая быстрая открытая аптека
            if fastest_open_pharmacy is None or option["delivery_option"]["eta"] < \
                    fastest_open_pharmacy["delivery_option"]["eta"]:
                fastest_open_pharmacy = option
                if not pharmacy_closes_soon:
                    alternative_fastest_option = None
                else:
                    logger.info(
                        f"Step 4.1: Pharmacy {source['code']} closes soon, looking for an alternative fastest pharmacy")
                    # Ищем самую быструю аптеку, которая не закрывается скоро
                    if not alternative_fastest_option:
                        for alt_option in delivery_data:
                            alt_pharmacy = alt_option.get("pharmacy", {})
                            alt_source = alt_pharmacy.get("source", {})
                            alt_closes_at = alt_source.get("closes_at")
                            alt_opens_at = alt_source.get("opens_at")
                            alt_opening_hours = alt_source.get("opening_hours", "")

                            alt_pharmacy_closes_soon = is_pharmacy_open_soon(alt_closes_at, alt_opens_at, alt_opening_hours)
                            alt_pharmacy_closed = is_pharmacy_closed(alt_closes_at, alt_opens_at, alt_opening_hours)

                            # Логика для поиска самой быстрой альтернативы, которая не закрывается скоро
                            if not alt_pharmacy_closes_soon and not alt_pharmacy_closed and \
                                    (alternative_fastest_option is None or alt_option["delivery_option"]["eta"] <
                                     alternative_fastest_option["delivery_option"]["eta"]):
                                logger.info(
                                    f"Step 5.1: Found alternative_fastest_option with code {alt_source.get('code')}, works longer than 1 hour, and eta {alt_option['delivery_option']['eta']}")
                                alternative_fastest_option = alt_option

    # Второй проход для анализа закрытых аптек с учетом уже выбранных открытых аптек
    for option in delivery_data:
        pharmacy = option.get("pharmacy", {})
        source = pharmacy.get("source", {})
        closes_at = source.get("closes_at")
        opens_at = source.get("opens_at")
        opening_hours = source.get("opening_hours", "")

        if 'code' not in source:
            continue

        pharmacy_closed = is_pharmacy_closed(closes_at, opens_at, opening_hours)

        if pharmacy_closed and cheapest_open_pharmacy:
            logger.info(
                f"Checking closed pharmacy {source['code']} with total price {option['total_price']} against cheapest_open_pharmacy: {cheapest_open_pharmacy['total_price']}")

            if option["total_price"] <= cheapest_open_pharmacy["total_price"] * 0.7:
                if cheapest_closed_pharmacy is None or option["total_price"] < cheapest_closed_pharmacy["total_price"]:
                    cheapest_closed_pharmacy = option

            logger.info(f"Closed pharmacy {source['code']} is not 30% cheaper than the open one.")

        if pharmacy_closed and fastest_open_pharmacy:
            logger.info(
                f"Checking closed pharmacy {source['code']} with eta {option['delivery_option']['eta']} against fastest_open_pharmacy eta: {fastest_open_pharmacy['delivery_option']['eta']}")

            if option["delivery_option"]["eta"] <= fastest_open_pharmacy["delivery_option"]["eta"] * 0.7:
                if fastest_closed_pharmacy is None or option["delivery_option"]["eta"] < \
                        fastest_closed_pharmacy["delivery_option"]["eta"]:
                    fastest_closed_pharmacy = option

            logger.info(f"Closed pharmacy {source['code']} is not 30% faster than the open one.")


    if cheapest_closed_pharmacy and cheapest_open_pharmacy:
        logger.info("Step 7: Returning both cheapest open and cheapest closed pharmacies due to 30% discount")
        return {
            "cheapest_delivery_option": cheapest_open_pharmacy,
            "alternative_cheapest_option": cheapest_closed_pharmacy,
            "fastest_delivery_option": fastest_open_pharmacy,
            "alternative_fastest_option": fastest_closed_pharmacy
        }

    logger.info(
        f"Step 8: Returning the standard results"
    )
    return {
        "cheapest_delivery_option": cheapest_open_pharmacy,
        "alternative_cheapest_option": alternative_cheapest_option,
        "fastest_delivery_option": fastest_open_pharmacy,
        "alternative_fastest_option": alternative_fastest_option
    }



#  функция для проверки выбранных на каждой стадии отбора аптек (сохраняет списки аптек в файлы локально)
def save_response_to_file(data, file_name='data.json'):
    try:
        # Проверяем, является ли data объектом JSONResponse
        if isinstance(data, JSONResponse):
            # Преобразуем тело JSONResponse в JSON-формат
            data = data.body.decode('utf-8')  # Декодируем из байтов в строку
            data = json.loads(data)  # Преобразуем строку в JSON-объект

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
                    "opening_hours": "Пн-Вс: 08:00-23:00",
                    "network_code": "apteka_chain_1",
                    "with_reserve": True,
                    "payment_on_site": True,
                    "kaspi_red": False,
                    "closes_at": "2024-10-21T18:00:00Z",
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
                                "base_price": 20,
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
                    # "opening_hours": "Круглосуточно",
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
