# acoGO! 2.0 — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/aco-ha/acogo?style=for-the-badge)](https://github.com/aco-ha/acogo/releases)
[![License](https://img.shields.io/github/license/aco-ha/acogo?style=for-the-badge)](LICENSE)

<p align="center">
  <img src="icon.png" width="180" height="180" alt="acoGO! Home Assistant Integration">
</p>

Интеграция для систем умных домофонов и модулей **ACO (acoGO! 2.0)** в **Home Assistant**.

Разработано на основе полного реверс-инжиниринга официального мобильного приложения `pl.aco.acogo` (v1.9.0) и облачного API `api.aco.com.pl`.

---

## Возможности

- 🚪 **Управление дверью (Замок 1)**: Надежное импульсное открытие основного электрозамка / калитки (`ezOpen`).
- 🚧 **Управление воротами (Замок 2 / F2)**: Открытие въездных ворот или шлагбаума через функцию F2 (`f2Open`).
- 🛡️ **Защита физической линии домофона**: Реализована безопасная последовательность перехвата и освобождения линии (`receiveCall` ➔ задержка ➔ `open` ➔ `endCall`) с защитой от состояния гонки через `asyncio.Lock` для каждого устройства.
- 📹 **Переключение видеокамер**: Кнопка циклического переключения видеовходов вызывной панели (`video-sw`).
- 🔔 **Детекция звонков**: Бинарный сенсор входящего вызова (`busy` / `ready`) для мгновенного запуска автоматизаций Home Assistant (оповещения на телефон, умные колонки, подсветка).
- 📶 **Мониторинг состояния**: Сенсор доступности панели (Online / Offline), версии ПО и прошивки.
- 📷 **Камера вызывной панели**: Поддержка видеопотока через AWS Kinesis Video Streams WebRTC с автоматическим закрытием сессии при неактивности (защита от исчерпания облачных квот).
- ⚙️ **Удобная настройка через UI**: Авторизация по учетным данным портала myAco / acoGO с автоматической генерацией сессии устройства.

---

## Установка

### Вариант 1: Установка через HACS (рекомендуется)

1. Убедитесь, что у вас установлен [HACS](https://hacs.xyz/).
2. В интерфейсе Home Assistant перейдите в **HACS** ➔ **Интеграции**.
3. В правом верхнем углу нажмите меню с тремя точками ➔ **Пользовательские репозитории** (Custom repositories).
4. Добавьте URL вашего репозитория, выберите категорию **Интеграция** (Integration) и нажмите **Добавить**.
5. Найдите в поиске **acoGO! Intercom** и нажмите **Загрузить** (Download).
6. Перезагрузите Home Assistant.

### Вариант 2: Ручная установка

1. Скачайте архив репозитория.
2. Скопируйте папку `custom_components/acogo` в каталог `custom_components` вашей конфигурации Home Assistant (путь: `/config/custom_components/acogo`).
3. Перезагрузите Home Assistant.

---

## Настройка

1. В Home Assistant перейдите в **Настройки** ➔ **Устройства и службы** ➔ **Добавить интеграцию**.
2. Введите в поиске **acoGO! Intercom**.
3. В диалоговом окне укажите:
   - **Email / Имя пользователя** от аккаунта acoGO! / myAco.
   - **Пароль**.
4. Нажмите **Отправить**. Интеграция автоматически зарегистрирует защищенную сессию устройства в облаке ACO и добавит все обнаруженные панели домофонов и модули.

---

## Создаваемые сущности

Для каждой вызывной панели создаются следующие сущности:

| Домен | Имя | Описание |
|---|---|---|
| `lock` | `Door Lock` | Основной электрозамок двери / калитки |
| `lock` | `Gate (F2)` | Замок ворот / шлагбаума (функция F2) |
| `button` | `Open Door` | Импульсная кнопка открытия двери |
| `button` | `Open Gate` | Импульсная кнопка открытия ворот |
| `button` | `Switch Camera Video Input` | Переключение видеовхода камеры |
| `binary_sensor` | `Status` | Доступность панели в облаке (Online / Offline) |
| `binary_sensor` | `Incoming Call / Line Active` | Индикатор входящего звонка / занятости линии |
| `camera` | `Camera` | Камера вызывной панели (AWS KVS / WebRTC) |

---

## Примеры автоматизаций

### Оповещение на смартфон при звонке в домофон
```yaml
alias: "Домофон: Входящий звонок"
trigger:
  - platform: state
    entity_id: binary_sensor.aco_intercom_call
    to: "on"
action:
  - service: notify.notify
    data:
      title: "Звонок в домофон!"
      message: "Кто-то звонит в калитку."
      data:
        actions:
          - action: "OPEN_DOOR"
            title: "Открыть дверь"
```

### Открытие двери по кнопке из уведомления
```yaml
alias: "Домофон: Открыть по кнопке"
trigger:
  - platform: event
    event_type: mobile_app_notification_action
    event_data:
      action: "OPEN_DOOR"
action:
  - service: lock.unlock
    target:
      entity_id: lock.aco_intercom_door
```

---

## Лицензия

Проект распространяется под лицензией MIT.
