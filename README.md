# dl-geo-bot

## Проверка always-on геопингов

1. Запустите смену через `/start_shift` и дождитесь подтверждения, что смена активна.
2. Отправьте live location в чат бота (или обновляйте уже отправленную live location).
3. Убедитесь, что в логах бота появляются строки `LOCATION_UPDATE ...` и `PING_ADD ...`.
4. Проверьте, что в БД растёт счётчик пингов:
   - `SELECT COUNT(*) FROM oc_staff_shift_ping WHERE shift_id = <active_shift_id>;`

## Новые переменные окружения API

- `OC_API_BASE` — базовый URL OpenCart API **без** `index.php` (пример: `http://host:8080`).
- `OC_API_ADMIN_BASE` — базовый URL admin API **без** `index.php` (пример: `http://host:8080/admin`) для retry сценариев `admin_chat_ids`.
- `ADMIN_FORCE_CHAT_IDS` — fallback список chat_id (через запятую), используется если API не вернул валидный список.
