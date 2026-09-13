# Техническое Задание (ТЗ): Home Assistant Integration для acoGO! 2.0

## 1. Введение и цели проекта
Цель проекта — создание полнофункциональной кастомной интеграции для Home Assistant (`custom_components/acogo`), позволяющей управлять домофонными системами и модулями польского производителя ACO (ACO GO! 2.0), включая:
- Авторизацию в облаке ACO (`api.aco.com.pl`) с генерацией и сохранением постоянных учетных данных устройства (`devId`, `devicePassword`).
- Получение списка привязанных устройств (вызывных панелей, интеркомов, модулей acoGO!).
- Открытие двери (замок 1 / `ezOpen`).
- Открытие ворот / второй двери (замок 2 / `f2Open`).
- Переключение видеокамер вызывной панели (`video-sw`).
- Мониторинг статуса устройств (онлайн/оффлайн, занято/свободно, версии ПО).
- Управление реле модулей расширения IPIO.
- Интеграцию видеопотока (камера вызывной панели через AWS Kinesis Video Streams WebRTC / Snapshot).
- Отслеживание состояния вызовов (звонков).

---

## 2. Результаты реверс-инжиниринга приложения acoGO! (Android)

Приложение `pl.aco.acogo` (версия 1.9.0) построено на базе гибридного стека (Cordova/Capacitor + Angular). Все API-вызовы, протоколы взаимодействия и структуры данных были извлечены и исследованы из бандла приложения.

### 2.1. Базовые URL и параметры окружения
- **Cloud REST API Base URL**: `https://api.aco.com.pl/listener/v1`
- **User Portal**: `https://portal.acogo.pl`
- **AWS Region**: Преимущественно `eu-west-2` (определяется динамически в ответах API)
- **AWS Kinesis Video Endpoint**: `kinesisvideo.<region>.amazonaws.com`

---

### 2.2. Протокол аутентификации и регистрации клиента
ACO GO использует двухфакторную модель сессии устройства:
1. При первом входе клиент (приложение) генерирует уникальный `devId` (UUID v4) и отправляет запрос на регистрацию устройства:
   - **Метод**: `POST https://api.aco.com.pl/listener/v1/device`
   - **Заголовки**:
     ```http
     devId: <UUIDv4>
     userName: <login_email>
     userPassword: <password>
     Content-Type: application/json
     Accept: text/plain, */*
     ```
   - **Тело (JSON)**:
     ```json
     {
       "language": "en",
       "name": "Home Assistant acoGO",
       "localization": "",
       "firmware": "HA",
       "software": "1.0.0",
       "hardware": "HomeAssistant",
       "fcmToken": "",
       "model": 62
     }
     ```
     *(Примечание: `model: 62` обозначает Android-клиент, `63` — iOS. Панели домофонов имеют модели 64, 65 и т.д.)*
   - **Ответ (JSON)**:
     ```json
     {
       "status": "ok",
       "additionalInfo": {
         "devicePassword": "<Сгенерированный_пароль_сессии_устройства>",
         "awsThing": { ... }
       }
     }
     ```
2. Для всех последующих API-запросов авторизация выполняется через заголовки:
   ```http
   devId: <UUIDv4>
   devicePassword: <devicePassword>
   Content-Type: application/json
   Accept: text/plain, */*
   ```

---

### 2.3. Основные REST API эндпоинты

#### 1. Получение списка устройств
- **URL**: `GET /device-by-app`
- **Заголовки**: `devId`, `devicePassword`
- **Ответ**: Массив объектов устройств (`devId`, `name`, `status`, `role`, `model`, `firmware`, `software`, `supportedAddresses`, `dialDelay`, `awsThingName`, `additionalInfo`).
- *Фильтрация*: Устройства с `model: 62` или `63` — это мобильные приложения-клиенты, их необходимо исключать из списка панелей домофонов.

#### 2. Проверка состояния панели (готовность/вызов)
- **URL**: `POST /device/check-state`
- **Тело**: `{"devId": "<intercom_devId>"}`
- **Ответ**: `{"response": "ready"}` (готов), `"busy"` (линия занята / идет вызов), `"offline"` (панель недоступна).

#### 3. Открытие двери и ворот (`order`)
В приложении acoGO реализована специфическая логика отправки команд:
- **URL**: `POST /order?orderId=<ORDER_ID>`
- **Тело**:
  ```json
  {
    "address": null,
    "targetId": "<intercom_devId>"
  }
  ```
- **Идентификаторы команд (`orderId`)**:
  - `ezOpen` — Открытие основного электрозамка / двери 1.
  - `f2Open` — Открытие замка 2 / въездных ворот / шлагбаума (функция F2).
  - `receiveCall` — Ответ на вызов / установка сессии связи.
  - `rejectCall` — Сброс вызова.
  - `endCall` — Завершение сессии связи.
- **Алгоритм бескаллового открытия (Door Action Sequence)**:
  Если в момент нажатия кнопки вызов не активен, для физического срабатывания реле домофона acoGO эмулирует следующую цепочку:
  1. `POST /order?orderId=receiveCall`
  2. Задержка 3 секунды
  3. `POST /order?orderId=ezOpen` (или `f2Open`)
  4. Задержка 5 секунд
  5. `POST /order?orderId=endCall`
  Если вызов уже активен (панель звонит или линия занята), команда `ezOpen` / `f2Open` отправляется немедленно без предварительного `receiveCall`.

#### 4. Переключение видеокамер панели
- **URL**: `POST /order/video-sw`
- **Тело**: `{"targetId": "<intercom_devId>"}`
- Позволяет циклически переключать видеовходы вызывной панели.

#### 5. Управление модулями ввода-вывода IPIO
- **URL**: `GET /device/module/ipio/<intercom_devId>` — список каналов IPIO.
- **URL**: `GET /device/module/ipio/state/<intercom_devId>/<ipioId>` — текущий статус входов/выходов.
- **URL**: `POST /order/ipio` — переключение реле модуля IPIO.

#### 6. Видеотрансляция (Camera Preview / WebRTC)
- **Запрос сессии**: `POST /preview/request`
- **Тело**: `{"devId": "<intercom_devId>", "previewType": "video-only"}`
- **Ответ**:
  ```json
  {
    "params": {
      "callId": "...",
      "channelARN": "arn:aws:kinesisvideo:<region>:...:channel/...",
      "aws": {
        "region": "eu-west-2",
        "access key ID": "...",
        "secret access key ID": "...",
        "session token": "..."
      }
    }
  }
  ```
- **Завершение сессии**: `POST /preview/end` с пустым телом.

---

## 3. Архитектура интеграции для Home Assistant

### 3.1. Структура каталога `custom_components/acogo/`
```
custom_components/acogo/
├── __init__.py           # Инициализация интеграции, DataUpdateCoordinator
├── manifest.json         # Манифест интеграции HA
├── config_flow.py        # Настройка через UI HA (ввод логина и пароля, генерация devId)
├── const.py              # Константы (домены, типы команд, тайминги)
├── api.py                # Асинхронный клиент AcoGoApiClient (aiohttp, обработка ошибок, повторы)
├── coordinator.py        # AcoGoDataUpdateCoordinator (периодический опрос состояния)
├── lock.py               # Сущности Lock: Door 1 (ezOpen) и Gate/Door 2 (f2Open)
├── button.py             # Сущности Button: Импульсное открытие двери/ворот, переключение камеры
├── binary_sensor.py      # Сенсоры: статус подключения (Online), статус линии (Busy/Ringing)
├── switch.py             # Переключатели каналов модуля IPIO (при наличии)
├── camera.py             # Камера: WebRTC / Preview
└── strings.json          # Локализация
```

### 3.2. Требования к надежности и безопасности
1. Все учетные данные (`password`, `devicePassword`, `devId`) надежно хранятся в `ConfigEntry`.
2. HTTP-клиент использует `aiohttp.ClientSession` Home Assistant (`async_get_clientsession`) с таймаутами и обработкой сетевых исключений.
3. Команды открытия дверей защищены от флуда и состояния гонки (asyncio.Lock для последовательности `receiveCall` -> `ezOpen` -> `endCall`).
4. При сбросе сессии (401 Unauthorized) интеграция должна автоматически перерегистрировать сессию без участия пользователя.
