# Запуск бота на своём VDS (Waicore) — пошагово

Инструкция рассчитана на человека, который умеет зайти на сервер по SSH и набрать команду, но не настраивал серверы глубже. Команды можно копировать целиком. Строки, которые начинаются с `#`, — комментарии, их набирать не нужно.

Порядок такой: проверить, доступен ли вам API → получить ключ и положить деньги → создать бота в Telegram → осмотреть сервер, ничего не меняя → поставить Docker, если его нет, и убедиться, что VPN жив → запустить бота.

## 0. Нужна ли постоянно включённая машина

Да: бот — это программа, которая должна работать круглосуточно. Она постоянно спрашивает Telegram о новых сообщениях и пересылает их в Claude. Если она выключена, бот молчит. Telegram хранит неполученные сообщения до 24 часов и отдаст их, когда бот снова запустится.

Поэтому бот и ставится на VDS: сервер работает круглосуточно, а ваш компьютер можно выключать. Ресурсов бот берёт немного: около 200 МБ памяти в покое и до ~450 МБ, когда обрабатывает большой PDF. Процессор почти не нужен. Ваш VPN при этом продолжает работать как раньше: бот не открывает портов и с VPN не пересекается. Единственная точка, где Docker может задеть VPN, разобрана в шаге 6.

## 1. ⚠️ До оплаты: проверьте, можно ли вам пользоваться Claude API

Anthropic официально обслуживает только страны из списка: https://www.anthropic.com/supported-countries. **Россия и Беларусь в нём не значатся** (проверено 10.10.2026). Германия, Финляндия, Нидерланды, Казахстан, Грузия, Армения — есть.

Важно, что правило касается не только IP-адреса сервера. На той же странице сказано, что любая страна вне списка не поддерживается. В том числе не поддерживается использование людьми, которые физически находятся в неподдерживаемой стране, и организациями, зарегистрированными там, — независимо от того, где стоит сервер. Ещё одно: если ваш VDS у Waicore в московской локации, запросы к API пойдут из России.

Кредиты Anthropic не возвращаются, поэтому решите этот вопрос до пополнения баланса. Если запрос идёт из неподдерживаемой страны, API отвечает ошибкой: официальная документация упоминает 403 для региональных ограничений, а пользователи сообщали и о 400 с текстом про unsupported countries. В обоих случаях бот покажет текст ошибки.

## 2. API-ключ Anthropic: что это и как получить

**Что это.** API-ключ — это пароль, по которому серверы Anthropic узнают вашу программу и списывают деньги с вашего счёта. Выглядит как `sk-ant-api03-...`. Кто знает ключ, тот тратит ваши деньги, поэтому храните его только в файле `.env` на сервере и в менеджере паролей. Утёкший ключ удалите в Console и создайте новый.

**Как платить.** Claude API работает по предоплате: вы покупаете кредиты, и каждый запрос их расходует. Когда кредиты кончаются, запросы перестают проходить. По [справке Anthropic](https://support.claude.com/en/articles/8977456) кредиты сгорают через год после покупки и не возвращаются. Минимальную сумму покупки покажет Console: в документации она не указана. Подписка Claude Pro/Max — отдельный продукт, к API она не относится.

Шаги:

1. Зайдите на https://platform.claude.com и зарегистрируйтесь.
2. **Settings → Billing** — купите кредиты. Для начала хватит $10–20: через несколько дней команда `/usage` в боте покажет реальный темп расходов.
3. Там же **Auto-reload** (автопополнение) оставьте выключенным: тогда потратить больше, чем лежит на балансе, невозможно.
4. Там же **Spend limits → Set limit** — поставьте месячный потолок, например $20. Когда он будет достигнут, API перестанет принимать запросы до конца месяца, а бот сообщит об этом.
5. **Settings → API keys → Create key.** Назовите ключ, например, `telegram-bot`, и скопируйте его. **Ключ показывается один раз.** Если потеряете, создайте новый.

Новые аккаунты Anthropic сначала получают пониженные лимиты (уровень Evaluation). Со временем они повышаются автоматически.

## 3. Бот в Telegram

1. Откройте в Telegram [@BotFather](https://t.me/BotFather) и отправьте `/newbot`.
2. Введите имя бота (любое, его увидят в чате), затем username: латиницей и обязательно с окончанием `bot`, например `my_claude_helper_bot`.
3. BotFather пришлёт **токен** вида `1234567890:AAH...`. Это пароль от бота, никому его не показывайте.
4. Если токен утёк: @BotFather → `/mybots` → ваш бот → **API Token → Revoke current token**. Потом впишите новый токен в `.env` на сервере (шаг 8) и перезапустите бота.

Найти бота по username может кто угодно, но отвечать он будет только тем, чьи Telegram ID вы разрешите.

## 4. Заходим на сервер

Понадобятся IP-адрес сервера, логин (обычно `root`) и пароль. Обычно провайдер присылает их в письме об активации или показывает в личном кабинете на странице услуги. Документацию Waicore проверить не удалось: их сайт с инструкциями был недоступен.

**Подключайтесь к серверу напрямую, с выключенным VPN.** Если вы зайдёте по SSH через VPN, который работает на этом же сервере, то при поломке VPN оборвётся и SSH, и вы не сможете ничего исправить. Если в кабинете Waicore есть веб-консоль (VNC), запомните, где она: это запасной вход на крайний случай.

- **Windows 10/11:** откройте PowerShell (Пуск → «PowerShell»).
- **macOS / Linux:** откройте «Терминал».

```bash
ssh root@IP_СЕРВЕРА
```

При первом подключении появится вопрос `Are you sure you want to continue connecting (yes/no)?` — напечатайте `yes`. При вводе пароль не отображается — так и должно быть. Выйти с сервера можно командой `exit`.

В инструкции команды пишутся с `sudo`. Если вы зашли как `root` и система отвечает `sudo: command not found`, просто убирайте слово `sudo` из команд.

## 5. Осмотр сервера (ничего не меняет)

Выполните по очереди и запишите ответы: от них зависит, что делать дальше.

```bash
# Система: ожидается Ubuntu 22.04/24.04 или Debian 12/13
cat /etc/os-release | head -4

# Память и диск: нужно хотя бы ~700 МБ свободной памяти и 3 ГБ на диске
free -h
df -h /

# Тип виртуализации: для Docker нужен kvm (или другая полная виртуализация), не openvz/lxc
systemd-detect-virt

# Есть ли уже Docker (его часто ставят сами VPN: Amnezia, Outline, 3x-ui в Docker)
docker --version
docker compose version
sudo docker ps --format '{{.Names}}  {{.Image}}'

# Какой VPN работает как системная служба
systemctl list-units --type=service --state=running | grep -iE 'wg|wireguard|x-ui|xray|amnezia|outline|openvpn|shadowsocks|sing-box|hysteria|marzban'

# Включена ли пересылка пакетов (её используют VPN вроде WireGuard/OpenVPN)
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
```

Как читать ответы:

- `free -h`, строка `Mem`, колонка `available`. Если там меньше ~700 МБ, в шаге 8 поставьте `BOT_MEM_LIMIT=600m` и не присылайте боту большие PDF.
- `systemd-detect-virt` вернул `openvz` или `lxc` — Docker там, скорее всего, не заработает. Используйте вариант без Docker (приложение в конце).
- `docker --version` что-то напечатал → **Docker уже есть, идите в шаг 6А**. Ответ `command not found` → **шаг 6Б**.

## 6. Docker

**Docker** запускает программу в изолированном контейнере вместе со всеми её зависимостями: на сервер не нужно ставить Python и библиотеки, а бот не может испортить систему. **Docker Compose** запускает контейнер по описанию из файла `docker-compose.yml`, который лежит в репозитории.

### 6А. Docker уже установлен (скорее всего, его поставил VPN)

**Ничего не переустанавливайте и не удаляйте.** Ваш VPN, возможно, работает в этом Docker, и переустановка его сломает.

Проверьте только, есть ли Compose:

```bash
docker compose version
```

Если напечатана версия — переходите к шагу 7. Если `docker: 'compose' is not a docker command`, узнайте, откуда стоит Docker:

```bash
dpkg -l | grep -E '^ii +(docker-ce|docker\.io) '
```

- Строка с `docker-ce` (официальный репозиторий Docker) — поставьте `sudo apt install -y docker-compose-plugin`.
- Строка с `docker.io` на Ubuntu — поставьте `sudo apt install -y docker-compose-v2`.
- Строка с `docker.io` на Debian — пакет Compose там называется по-разному в разных версиях, и я этот путь не проверял. Самое надёжное — запустить бота без Docker (приложение в конце).

Файл `/etc/docker/daemon.json` при этом не трогайте: после его правки нужен перезапуск Docker, а вместе с ним перезапустятся и контейнеры VPN.

### 6Б. Docker не установлен — ставим из официального репозитория

**Сначала о риске для VPN.** Если Docker при запуске сам включает пересылку пакетов (`ip_forward`), он заодно ставит правило «не пересылать чужие пакеты»: политику цепочки `FORWARD` в iptables переводит в `DROP` (так написано в [документации Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/)). Что это значит для вас, смотрите по результату `sysctl` из шага 5:

- `net.ipv4.ip_forward = 1` — пересылку уже включил ваш VPN, и Docker IPv4-политику не трогает.
- `net.ipv4.ip_forward = 0` — ваш VPN, судя по всему, работает как прокси (Xray/3x-ui, Outline/Shadowsocks) и не пересылает пакеты через ядро. Тогда политика `DROP` его не касается. Это мой вывод из того, как устроены такие прокси, а не утверждение из документации.

В обоих случаях после установки обязательно проверьте VPN (шаг 6В).

Установка — официальные команды из [документации Docker](https://docs.docker.com/engine/install/ubuntu/). Переменная `$ID` подставит `ubuntu` или `debian`, так что команды подходят для обеих систем:

```bash
# 1. Ключ и адрес репозитория Docker
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
sudo curl -fsSL https://download.docker.com/linux/$ID/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update

# 2. Сам Docker и Compose
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 3. Проверка: должно напечататься «Hello from Docker!»
sudo docker run --rm hello-world
```

Скрипт `get.docker.com` из интернета не используйте: сама Docker рекомендует его только для тестовых машин.

### 6В. Проверьте, что VPN жив

Подключитесь к VPN с телефона или компьютера и откройте любой сайт. Если всё работает — переходите к шагу 7.

**Если VPN перестал работать после установки Docker**, проверьте политику пересылки:

```bash
sudo iptables -S FORWARD | head -1
```

Если ответ `-P FORWARD DROP`, верните прежнее поведение. Эта опция Docker [как раз для этого и предназначена](https://docs.docker.com/engine/network/packet-filtering-firewalls/#docker-on-a-router). Сначала убедитесь, что файла настроек Docker ещё нет: `sudo cat /etc/docker/daemon.json` должна ответить `No such file or directory`. Если файл есть, не перезаписывайте его, а допишите `"ip-forward-no-drop": true` внутрь фигурных скобок через `sudo nano`.

```bash
echo '{ "ip-forward-no-drop": true }' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
sudo iptables -P FORWARD ACCEPT
# если VPN раздаёт клиентам IPv6 и `sudo ip6tables -S FORWARD | head -1` тоже показывает DROP:
sudo ip6tables -P FORWARD ACCEPT
```

Если не помогло, или VPN настроен правилами `nft` (Docker поддерживает только правила, созданные через `iptables`), остановите Docker командой `sudo systemctl disable --now docker docker.socket` и перезагрузите сервер: `sudo reboot`. Затем запускайте бота без Docker (приложение в конце).

## 7. Код бота на сервере

```bash
# git обычно уже есть; если нет — поставится
git --version || sudo apt install -y git

cd ~
git clone https://github.com/RegenTaller/tg-Claude-bot.git
cd tg-Claude-bot
```

Репозиторий публичный: код может прочитать кто угодно, а ваши секреты — нет, потому что они лежат только в `.env` на сервере. **Никогда не коммитьте `.env`.**

Все дальнейшие команды выполняются из этой папки. Если вы перезашли на сервер, сначала выполните `cd ~/tg-Claude-bot`.

## 8. Настройки: файл `.env`

```bash
cp .env.example .env
nano .env
```

Откроется простой редактор. Заполните:

| Параметр | Что вписать |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен от BotFather (шаг 3). |
| `ANTHROPIC_API_KEY` | Ключ `sk-ant-...` (шаг 2). |
| `ALLOWED_USER_IDS` | Пока оставьте пустым — свой ID узнаете в шаге 10. |
| `ADMIN_USER_IDS` | Тоже пока пусто, потом — только ваш ID. Админ может выдавать доступ (`/allow`) и видит расход всех в `/usage`. |
| `DEFAULT_MODEL`, `DEFAULT_EFFORT` | Модель и effort для новых пользователей. `claude-opus-5-5` и `medium` — разумный старт. |
| `MAX_TOKENS` | Длина ответа вместе с размышлениями. Оставьте `64000`. |
| `USER_MONTHLY_LIMIT_USD` | Месячный лимит в долларах на каждого не-админа, например `10`. `0` — без лимита. |
| `HISTORY_RETENTION_DAYS` | Через сколько дней удалять брошенные диалоги вместе с файлами. По умолчанию `30`. |
| `BOT_MEM_LIMIT` | Потолок памяти контейнера. `1g`, а если на сервере меньше ~1 ГБ свободной памяти — `600m`. |

В nano: стрелки двигают курсор, вставить из буфера — правая кнопка мыши (или Ctrl+Shift+V). Сохранить — **Ctrl+O**, затем **Enter**. Выйти — **Ctrl+X**.

Закройте файл от чужих глаз:

```bash
chmod 600 .env
```

## 9. Папка для базы

Бот в контейнере работает не от root, а от отдельного пользователя с номером 10001. Это мера безопасности. Поэтому папку для базы нужно отдать ему:

```bash
mkdir -p data
sudo chown 10001:10001 data
```

Если забыть этот шаг, бот не запустится, а в логе будет «Не открывается база … проверьте права на папку data».

## 10. Первый запуск

```bash
sudo docker compose up -d --build
```

Первая сборка займёт 1–3 минуты: Docker скачает Python и библиотеки. Потом проверьте, что всё работает:

```bash
sudo docker compose ps                # в колонке STATUS должно быть «Up …»
sudo docker compose logs --tail 30    # ищите строки «Start polling» и «Run polling for bot @…»
```

Дальше:

1. Напишите своему боту в Telegram `/id` — он ответит вашим Telegram ID: «это приватный бот», и ниже номер.
2. Впишите этот номер в `.env` в `ALLOWED_USER_IDS` и `ADMIN_USER_IDS` (`nano .env`, сохранить, выйти).
3. Перезапустите бота с новыми настройками:
   ```bash
   sudo docker compose up -d --force-recreate
   ```
4. Отправьте боту `/start`, а потом любой вопрос. Ответ должен появиться и дописываться на глазах.
5. Ещё раз проверьте, что VPN работает.

**Второй человек.** Пусть он отправит боту `/id` и перешлёт номер вам, а вы отправьте боту `/allow НОМЕР`. Перезапуск не нужен. Отозвать доступ — `/deny НОМЕР`.

## 11. Повседневные команды

Все — из папки бота (`cd ~/tg-Claude-bot`):

| Задача | Команда |
|---|---|
| Смотреть логи в реальном времени | `sudo docker compose logs -f --tail 100` (выйти — Ctrl+C, бот при этом продолжит работать) |
| Последние 200 строк логов | `sudo docker compose logs --tail 200` |
| Работает ли бот | `sudo docker compose ps` |
| Перезапустить | `sudo docker compose restart` |
| Применить изменения в `.env` | `sudo docker compose up -d --force-recreate` |
| Остановить / запустить | `sudo docker compose stop` / `sudo docker compose start` |
| **Обновить бота до новой версии** | `git pull && sudo docker compose up -d --build && sudo docker image prune -f` |

После перезагрузки сервера бот поднимется сам: в `docker-compose.yml` стоит `restart: unless-stopped`, а служба Docker запускается при старте системы. Проверить это можно командой `systemctl is-enabled docker` — должно быть `enabled`.

Последняя команда обновления (`docker image prune -f`) удаляет только старые безымянные образы бота, оставшиеся от прошлой сборки. Образы и данные VPN она не трогает.

## 12. Чего не делать, чтобы не сломать VPN

- Не запускайте `docker system prune -a`, `docker volume prune`, `docker rm -f $(docker ps -aq)` — они могут удалить контейнеры и данные VPN (у Amnezia и Outline всё хранится в Docker).
- Не выполняйте `docker compose down` в чужих папках — только в `~/tg-Claude-bot`.
- Не переустанавливайте Docker и не правьте `/etc/docker/daemon.json` без необходимости: перезапуск Docker перезапускает и контейнеры VPN.
- Не добавляйте в `docker-compose.yml` раздел `ports:` — боту порты не нужны.
- `sudo apt upgrade` может обновить Docker и перезапустить его. VPN-клиенты при этом переподключатся. Делайте это, когда удобно.

## 13. Резервная копия

История диалогов, настройки и расход — в одном файле `data/bot.sqlite3`:

```bash
sudo docker compose stop
sudo cp data/bot.sqlite3 ~/bot-backup-$(date +%F).sqlite3
sudo docker compose start
```

Скачать копию на свой компьютер (команда выполняется на компьютере, не на сервере):

```bash
scp root@IP_СЕРВЕРА:~/bot-backup-*.sqlite3 .
```

## 14. Если что-то пошло не так

| Симптом | Причина и что делать |
|---|---|
| В логах `Не открывается база /data/bot.sqlite3` | Не выполнен шаг 9: `sudo chown 10001:10001 data`, затем `sudo docker compose restart`. |
| `Нужны TELEGRAM_BOT_TOKEN и ANTHROPIC_API_KEY`, или ошибка про другой параметр | Опечатка в `.env`. Сообщение называет параметр. Исправьте и выполните `sudo docker compose up -d --force-recreate`. |
| В логах `TelegramConflictError` / `terminated by other getUpdates request` | Бот с этим токеном запущен где-то ещё, например на вашем компьютере. Остановите вторую копию. |
| В логах `TelegramUnauthorizedError` | Неверный или отозванный токен бота. |
| Бот отвечает «Это приватный бот» | Вашего ID нет в `ALLOWED_USER_IDS`. Шаг 10, пункты 1–3. |
| «API-ключ Anthropic недействителен» | Ключ скопирован с ошибкой или удалён в Console. |
| «закончились деньги» | Пополните баланс: Console → Billing. |
| «Достигнут лимит расходов, установленный в Anthropic Console» | Сработал ваш spend limit. Поднимите его или ждите следующего месяца. |
| Ошибка про unsupported countries / 403 | См. шаг 1. |
| `sudo docker compose ps` показывает `Restarting`, в логах ничего нет | Контейнер убивается из-за нехватки памяти. Проверьте `free -h`, уменьшите нагрузку или поднимите `BOT_MEM_LIMIT`. |
| Бот долго «думает» на effort `max` | Это нормально: на высоком effort ответ может идти минутами. |
| VPN перестал работать после установки Docker | Шаг 6В. |

## Приложение: запуск без Docker (systemd)

Используйте этот вариант, если Docker не ставится (openvz/lxc) или не хочется рисковать настройками сети VPN: обычная программа на Python не меняет правила iptables. Нужен Python 3.10 или новее. В Ubuntu 22.04 и 24.04 и в Debian 12 он уже есть.

```bash
sudo apt install -y python3-venv git
sudo useradd --system --home /opt/tg-claude-bot --shell /usr/sbin/nologin tgbot
sudo git clone https://github.com/RegenTaller/tg-Claude-bot.git /opt/tg-claude-bot
cd /opt/tg-claude-bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # заполнить, как в шаге 8; BOT_MEM_LIMIT здесь не используется
sudo mkdir -p data
sudo chown -R tgbot:tgbot data .env
sudo chmod 600 .env
```

Создайте службу:

```bash
sudo tee /etc/systemd/system/tg-claude-bot.service <<'EOF'
[Unit]
Description=Telegram-бот для Claude
After=network-online.target
Wants=network-online.target

[Service]
User=tgbot
WorkingDirectory=/opt/tg-claude-bot
EnvironmentFile=/opt/tg-claude-bot/.env
Environment=DB_PATH=/opt/tg-claude-bot/data/bot.sqlite3
ExecStart=/opt/tg-claude-bot/.venv/bin/python -m bot.main
Restart=always
RestartSec=5
MemoryMax=1G
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/tg-claude-bot/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now tg-claude-bot
```

| Задача | Команда |
|---|---|
| Статус | `sudo systemctl status tg-claude-bot` |
| Логи в реальном времени | `sudo journalctl -u tg-claude-bot -f` |
| Перезапуск (в том числе после правки `.env`) | `sudo systemctl restart tg-claude-bot` |
| Обновление | `cd /opt/tg-claude-bot && sudo git pull && sudo .venv/bin/pip install -r requirements.txt && sudo systemctl restart tg-claude-bot` |
