# dl-geo-bot

## Проверка always-on геопингов

1. Запустите смену через `/start_shift` и дождитесь подтверждения, что смена активна.
2. Отправьте live location в чат бота (или обновляйте уже отправленную live location).
3. Убедитесь, что в логах бота появляются строки `LOCATION_UPDATE ...` и `PING_ADD ...`.
4. Проверьте, что в БД растёт счётчик пингов:
   - `SELECT COUNT(*) FROM oc_staff_shift_ping WHERE shift_id = <active_shift_id>;`
