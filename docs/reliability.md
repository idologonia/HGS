# Надійність HGS: що перевірено (Етап 7)

Кожен пункт підтверджено автотестом (`tests/`). Тести запускаються на Windows і Linux у GitHub Actions.

| Сценарій | Що відбувається | Тест |
|----------|-----------------|------|
| Аварійне завершення ядра («End task», збій) | процеси ігор завершуються (Windows: Job Object; також самі виходять, коли зник зв'язок з ядром); наступний запуск показує попередження і **не** запускає сервери сам | `reliability/test_reliability.py::test_killing_the_core_leaves_no_game_processes` |
| Закриття GUI | ядро помічає закритий канал керування, зберігає стан ігор, коректно зупиняє їх | `test_closing_control_pipe_stops_core_gracefully` |
| Падіння процесу гри | гравці отримують повідомлення, автоперезапуск (до 3 разів за 10 хв, з паузами), далі стан «Помилка»; інші ігри не зачеплені | `integration/test_workers.py::test_worker_crash_restarts_and_isolation` |
| Зависання гри | перевірка «пульсу» кожні 10 с; після 3 пропусків процес примусово завершується і діє політика перезапуску | `test_frozen_worker_is_killed_by_heartbeat` |
| Помилка в коді гри (виняток) | записується в лог гри, сервер і з'єднання працюють далі | `test_logic_game_state_and_room_isolation` |
| Втрата інтернету у гравця | сесія чекає 30 с; SDK повертається в ту саму кімнату одноразовим токеном | `test_internet.py::test_resume_after_dropped_connection`, браузерний `test_sdk_resumes_session_after_network_drop` |
| Падіння тунелю | cloudflared перезапускається (до 3 разів за 10 хв), далі помилка в GUI | `test_crashing_tunnel_is_restarted_then_marked_failed` |
| Пошкоджені повідомлення | ~400 видів зіпсованих кадрів (випадкові байти, обрізаний JSON, неправильні типи, глибока вкладеність): сервер відповідає помилкою або розриває з'єднання, інші гравці не помічають | `test_fuzzed_messages_do_not_break_the_server` |
| Флуд, завеликі повідомлення, підбір паролів | обмеження частоти, розміру, блокування IP, журнал безпеки | `integration/test_core.py`, `test_internet.py` |
| Зупинка сервера під час гри | попередження гравцям, пауза, збереження стану, відключення | `test_stop_with_players_sends_shutdown_notice`, `test_saves_survive_restart` |
| Сотні підключень/відключень | без витоків пам'яті, сесій, лічильників, місць у кімнатах | `test_connection_churn_does_not_leak` |
| Затримка | відповіді не чекають TCP-підтверджень (вимкнено алгоритм Nagle): медіана < 20 мс | `test_round_trip_latency_is_low` |
| Пошкоджені файли | зламаний опис однієї гри не заважає запуску HGS; зіпсований знімок пропускається на користь старішого; зламаний settings.json не перезаписується, GUI показує причину | `test_corrupted_files_are_handled`, `test_corrupted_snapshot_falls_back_to_older`, `test_invalid_settings_file_is_reported_not_replaced` |
| Зайнятий порт | зрозуміла помилка з номером порту | `test_busy_port_gives_a_clear_error` |
| Інтерфейс під навантаженням | цикл подій GUI не зупиняється довше 0,5 с при 100+ повідомленнях/с і потоці логів | `gui/test_gui.py::test_gui_stays_responsive_under_load` |

## Що не перевірено автоматично

* **Вимкнення Windows** під час роботи серверів (`commitDataRequest` → коректна зупинка): потребує справжнього
  сеансу Windows. Навіть якщо Windows не дасть часу, Job Object завершить процеси ігор, а наступний запуск HGS
  попередить про некоректне завершення.
* **Багатогодинна робота** (soak-тест): у CI — лише короткий тест на витоки (440 підключень).
* **Справжній тунель Cloudflare і DuckDNS** — перевіряються вручну на вашому ПК
  ([інструкція](guides/internet-access.md)).
