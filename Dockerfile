# Используем Python image
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы приложения
COPY . /app

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Открываем порт (уже не указываем конкретное значение, он будет динамическим через ENV)
EXPOSE ${PORT}

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}