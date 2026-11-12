"""Network page: access mode, tunnel, direct (TLS/DuckDNS), diagnostics, player address."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QRadioButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from hgs.gui.theme.dark import GREEN, MUTED, RED, YELLOW
from hgs.gui.widgets.common import Banner

if TYPE_CHECKING:
    from hgs.gui.core_client import CoreClient

MODES = [
    ("local", "Local — лише цей ПК", "Для тестування на своєму комп'ютері. Ніхто ззовні не може підключитися."),
    ("lan", "LAN — домашня мережа", "Гравці з вашої Wi-Fi/локальної мережі. Без шифрування: ігри з GitHub Pages "
                                     "(https) так підключитися не зможуть."),
    ("tunnel", "Tunnel — інтернет через тунель (рекомендовано)",
     "Працює навіть без публічної IP і при CGNAT. Дає готову https/wss-адресу для ігор на GitHub Pages. "
     "Сервер лишається закритим для прямих підключень."),
    ("direct", "Direct — інтернет напряму", "Потрібні публічна IP (не CGNAT), проброс порту на роутері, домен "
                                            "і TLS-сертифікат. Для досвідчених користувачів."),
]
TUNNEL_STATES = {"stopped": ("Зупинено", MUTED), "starting": ("Запуск…", YELLOW), "connected": ("Підключено", GREEN),
                 "reconnecting": ("Перепідключення…", YELLOW), "failed": ("Помилка", RED)}


def _row(*widgets: QWidget) -> QHBoxLayout:
    h = QHBoxLayout()
    for w in widgets:
        h.addWidget(w)
    h.addStretch(1)
    return h


class NetworkPage(QWidget):
    def __init__(self, client: "CoreClient", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.view: dict[str, Any] | None = None
        self.setObjectName("Page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Network")
        title.setObjectName("PageTitle")
        outer.addWidget(title)
        self.banner = Banner()
        outer.addWidget(self.banner)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)
        lay.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # -- address for players --------------------------------------------------
        addr = QGroupBox("Адреса сервера для гравців")
        al = QVBoxLayout(addr)
        self.player_url = QLabel("—")
        self.player_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.player_url.setStyleSheet("font-size: 13pt; font-family: Consolas, monospace;")
        self.copy_url = QPushButton("Копіювати")
        self.copy_url.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.view["player_url"] or ""))
        al.addLayout(_row(self.player_url, self.copy_url))
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet(f"color: {YELLOW};")
        al.addWidget(self.warnings)
        lay.addWidget(addr)

        # -- mode -----------------------------------------------------------------
        mode_box = QGroupBox("Режим доступу")
        ml = QVBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QRadioButton] = {}
        for key, label, hint in MODES:
            rb = QRadioButton(label)
            self.mode_group.addButton(rb)
            self.mode_buttons[key] = rb
            ml.addWidget(rb)
            h = QLabel(hint)
            h.setObjectName("Muted")
            h.setWordWrap(True)
            h.setContentsMargins(24, 0, 0, 6)
            ml.addWidget(h)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        form = QFormLayout()
        form.addRow("Порт шлюзу:", self.port)
        ml.addLayout(form)
        self.apply_btn = QPushButton("Застосувати")
        self.apply_btn.setObjectName("Primary")
        self.apply_btn.clicked.connect(self.apply)
        ml.addLayout(_row(self.apply_btn))
        lay.addWidget(mode_box)

        # -- tunnel ---------------------------------------------------------------
        tb = QGroupBox("Тунель (Cloudflare)")
        tl = QVBoxLayout(tb)
        tform = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItem("Швидкий тунель (без акаунта, адреса змінюється)", "cloudflare_quick")
        self.provider.addItem("Tailscale Funnel (безкоштовно, постійна адреса, рідко блокується) — рекомендовано",
                              "tailscale_funnel")
        self.provider.addItem("Постійний тунель Cloudflare (акаунт + свій домен)", "cloudflare_named")
        self.provider.currentIndexChanged.connect(self._provider_changed)
        tform.addRow("Тип:", self.provider)
        self.named_url = QLineEdit()
        self.named_url.setPlaceholderText("https://play.example.com")
        tform.addRow("Публічна адреса:", self.named_url)
        self.token_btn = QPushButton("Вказати токен тунелю…")
        self.token_btn.clicked.connect(lambda: self._set_secret("cloudflare_tunnel_token", "Токен тунелю Cloudflare"))
        self.token_state = QLabel()
        tform.addRow("Токен:", self._hbox(self.token_btn, self.token_state))
        self.cf_path = QLineEdit()
        self.cf_path.setPlaceholderText("знайти автоматично")
        browse = QPushButton("Огляд…")
        browse.clicked.connect(self._browse_cloudflared)
        self.download_btn = QPushButton("Завантажити cloudflared")
        self.download_btn.clicked.connect(self._download)
        self.install_ts = QPushButton("Встановити Tailscale…")
        self.install_ts.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://tailscale.com/download/windows")))
        self.ts_path = QLineEdit()
        self.ts_path.setPlaceholderText("знайти автоматично")
        self.prog_label = QLabel("cloudflared:")
        tform.addRow(self.prog_label, self._hbox(self.cf_path, self.ts_path, browse, self.download_btn, self.install_ts))
        self.cf_found = QLabel()
        self.cf_found.setObjectName("Muted")
        self.cf_found.setWordWrap(True)
        tform.addRow("", self.cf_found)
        tl.addLayout(tform)
        self.tunnel_state = QLabel()
        self.tunnel_state.setStyleSheet("font-weight: 600;")
        self.tunnel_start = QPushButton("Запустити тунель")
        self.tunnel_start.setObjectName("Primary")
        self.tunnel_start.clicked.connect(lambda: self._call("POST", "/network/tunnel/start", None, "Тунель"))
        self.tunnel_stop = QPushButton("Зупинити тунель")
        self.tunnel_stop.clicked.connect(lambda: self._call("POST", "/network/tunnel/stop", None, "Тунель"))
        tl.addLayout(_row(self.tunnel_state, self.tunnel_start, self.tunnel_stop))
        self.tunnel_error = QLabel()
        self.tunnel_error.setWordWrap(True)
        self.tunnel_error.setOpenExternalLinks(True)
        self.tunnel_error.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.tunnel_error.setStyleSheet(f"color: {RED};")
        tl.addWidget(self.tunnel_error)
        self.limits = QLabel()
        self.limits.setWordWrap(True)
        self.limits.setObjectName("Muted")
        tl.addWidget(self.limits)
        lay.addWidget(tb)

        # -- publish the address on GitHub ------------------------------------------
        pb = QGroupBox("Автоматична адреса для ігор (GitHub)")
        pf = QFormLayout(pb)
        hint = QLabel("HGS записує поточну адресу сервера у файл у вашому публічному GitHub-репозиторії. Ігри з "
                      "HGS.start({server: \"github:власник/репозиторій\"}) знаходять сервер самі — друзям не потрібні "
                      "посилання. Адреса не є секретом: для гри все одно потрібен вхід.")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        pf.addRow(hint)
        from PySide6.QtWidgets import QCheckBox

        self.pub_enabled = QCheckBox("Публікувати адресу")
        pf.addRow(self.pub_enabled)
        self.pub_repo = QLineEdit()
        self.pub_repo.setPlaceholderText("idologonia/hgs-address")
        pf.addRow("Репозиторій:", self.pub_repo)
        self.pub_path = QLineEdit()
        self.pub_path.setPlaceholderText("hgs-server.json")
        pf.addRow("Файл:", self.pub_path)
        self.gh_btn = QPushButton("Вказати токен GitHub…")
        self.gh_btn.clicked.connect(lambda: self._set_secret("github_token", "Токен GitHub"))
        self.gh_state = QLabel()
        pf.addRow("Токен:", self._hbox(self.gh_btn, self.gh_state))
        self.pub_now = QPushButton("Зберегти й опублікувати")
        self.pub_now.setObjectName("Primary")
        self.pub_now.clicked.connect(self.save_and_publish)
        self.pub_state = QLabel()
        self.pub_state.setWordWrap(True)
        pf.addRow(self.pub_now, self.pub_state)
        self.game_line = QLabel()
        self.game_line.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.game_line.setStyleSheet("font-family: Consolas, monospace;")
        self.game_line.setWordWrap(True)
        pf.addRow("У грі:", self.game_line)
        lay.addWidget(pb)
        self.dirty = False  # the user changed fields that are not saved yet

        # -- direct ---------------------------------------------------------------
        db = QGroupBox("Пряме підключення (Direct)")
        dform = QFormLayout(db)
        self.public_host = QLineEdit()
        self.public_host.setPlaceholderText("myhgs.duckdns.org")
        dform.addRow("Домен / адреса:", self.public_host)
        self.public_port = QSpinBox()
        self.public_port.setRange(0, 65535)
        self.public_port.setSpecialValueText("як порт шлюзу")
        dform.addRow("Зовнішній порт:", self.public_port)
        self.cert = QLineEdit()
        self.key = QLineEdit()
        for edit, label in ((self.cert, "Сертифікат (PEM):"), (self.key, "Ключ (PEM):")):
            b = QPushButton("Огляд…")
            b.clicked.connect(lambda _=False, e=edit: self._browse_file(e))
            dform.addRow(label, self._hbox(edit, b))
        self.duck_domain = QLineEdit()
        self.duck_domain.setPlaceholderText("myhgs (для myhgs.duckdns.org)")
        self.duck_btn = QPushButton("Вказати токен DuckDNS…")
        self.duck_btn.clicked.connect(lambda: self._set_secret("duckdns_token", "Токен DuckDNS"))
        dform.addRow("DuckDNS:", self._hbox(self.duck_domain, self.duck_btn))
        self.duck_state = QLabel()
        self.duck_state.setObjectName("Muted")
        dform.addRow("", self.duck_state)
        lay.addWidget(db)

        # mark the form as edited, so periodic refreshes do not overwrite what the user typed
        for edit in (self.named_url, self.cf_path, self.ts_path, self.public_host, self.cert, self.key, self.duck_domain,
                     self.pub_repo, self.pub_path):
            edit.textEdited.connect(self._mark_dirty)
        for w in (self.port, self.public_port):
            w.valueChanged.connect(lambda _v: self._mark_dirty() if self.view is not None and not self._loading else None)
        self.pub_enabled.clicked.connect(self._mark_dirty)
        self.provider.activated.connect(self._mark_dirty)
        for b in self.mode_buttons.values():
            b.clicked.connect(self._mark_dirty)
        self._loading = False

        # -- diagnostics ------------------------------------------------------------
        diag = QGroupBox("Діагностика")
        dl = QFormLayout(diag)
        self.public_ip = QLabel("—")
        ip_btn = QPushButton("Визначити")
        ip_btn.clicked.connect(lambda: self._call("POST", "/network/public-ip", None, "Публічна IP"))
        dl.addRow("Публічна IP:", self._hbox(self.public_ip, ip_btn))
        self.cgnat = QLabel()
        self.cgnat.setWordWrap(True)
        dl.addRow("", self.cgnat)
        self.lan = QLabel("—")
        self.lan.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dl.addRow("Адреси в локальній мережі:", self.lan)
        self.check_btn = QPushButton("Перевірити доступність ззовні")
        self.check_btn.clicked.connect(self._check)
        self.check_result = QLabel()
        self.check_result.setWordWrap(True)
        dl.addRow(self.check_btn, self.check_result)
        lay.addWidget(diag)
        lay.addStretch(1)

    @staticmethod
    def _hbox(*widgets: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x)
        return w

    # -- data ---------------------------------------------------------------------
    def refresh(self) -> None:
        self.client.call("GET", "/network", None, self._on_view)

    def _mark_dirty(self, *_: Any) -> None:
        self.dirty = True
        self.apply_btn.setText("Застосувати (є незбережені зміни)")

    def _on_view(self, ok: bool, data: Any) -> None:
        if not ok:
            self.banner.show_message(f"Не вдалося отримати мережеві налаштування: {data}")
            return
        self.view = data
        self._fill_status(data)
        if self.dirty:
            return  # keep the user's unsaved input
        self._loading = True
        try:
            self._fill_form(data)
        finally:
            self._loading = False

    def _fill_form(self, data: dict) -> None:
        s = data["settings"]
        self.mode_buttons[s["mode"]].setChecked(True)
        if not self.port.hasFocus():
            self.port.setValue(data["gateway_port"])
        self.player_url.setText(data["player_url"] or "— (недоступно в поточному стані)")
        self.copy_url.setEnabled(bool(data["player_url"]))
        self.warnings.setText("\n".join("⚠ " + w for w in data["warnings"]))
        self.warnings.setVisible(bool(data["warnings"]))
        t = s["tunnel"]
        idx = self.provider.findData(t["provider"])
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(idx)
        self.provider.blockSignals(False)
        for edit, value in ((self.named_url, t.get("named_public_url")), (self.cf_path, t.get("cloudflared_path")),
                            (self.ts_path, t.get("tailscale_path")),
                            (self.public_host, s["direct"].get("public_host")), (self.cert, s["direct"].get("tls_cert")),
                            (self.key, s["direct"].get("tls_key")), (self.duck_domain, s["direct"].get("duckdns_domain"))):
            if not edit.hasFocus():
                edit.setText(value or "")
        self.public_port.setValue(s["direct"].get("public_port") or 0)
        pub = s.get("publish", {})
        self.pub_enabled.setChecked(bool(pub.get("enabled")))
        self.pub_repo.setText(pub.get("repo") or "")
        self.pub_path.setText(pub.get("path") or "hgs-server.json")
        self._provider_changed()

    def _fill_status(self, data: dict) -> None:
        s = data["settings"]
        tun = data["tunnel"]
        text, color = TUNNEL_STATES.get(tun["state"], (tun["state"], MUTED))
        self.tunnel_state.setText(f"● {text}" + (f" — {tun['url']}" if tun.get("url") else ""))
        self.tunnel_state.setStyleSheet(f"color: {color}; font-weight: 600;")
        err = tun.get("error") or ""
        import html as _html
        import re as _re

        err_html = _re.sub(r"(https://\S+)", r'<a href="\1" style="color:#4f8cff">\1</a>', _html.escape(err))
        self.tunnel_error.setText(err_html)
        # a link to allow Funnel is an instruction, not an error
        self.tunnel_error.setStyleSheet(f"color: {YELLOW if tun['state'] == 'starting' else RED};")
        self.tunnel_error.setVisible(bool(tun.get("error")))
        self.limits.setText("\n".join("• " + x for x in tun["limitations"]))
        found = tun.get("binary")
        if s["tunnel"]["provider"] == "tailscale_funnel":
            self.cf_found.setText(f"Знайдено: {found}" if found else "Tailscale не знайдено: натисніть «Встановити "
                                  "Tailscale…», встановіть програму, увійдіть в акаунт і поверніться сюди.")
        else:
            self.cf_found.setText(f"Знайдено: {found}" if found else "cloudflared не знайдено. Завантажте його кнопкою або "
                                  "вкажіть шлях до cloudflared.exe.")
        running = tun["state"] in ("starting", "connected", "reconnecting")
        in_tunnel = s["mode"] == "tunnel"
        self.tunnel_start.setEnabled(in_tunnel and not running and bool(found))
        self.tunnel_start.setToolTip("" if in_tunnel else "Спочатку виберіть режим Tunnel і натисніть «Застосувати»")
        self.tunnel_stop.setEnabled(running)
        has_token = "cloudflare_tunnel_token" in data.get("secrets_set", [])
        self.token_state.setText("✓ збережено" if has_token else "не вказано")
        self.duck_state.setText(data.get("duckdns") or "")
        pub = s.get("publish", {})
        self.gh_state.setText("✓ збережено" if "github_token" in data.get("secrets_set", []) else "не вказано")
        pv = data.get("publish") or {}
        self.pub_state.setText(pv.get("error") and f"✗ {pv['error']}" or pv.get("status") or "")
        self.pub_state.setStyleSheet(f"color: {RED};" if pv.get("error") else "")
        repo = pub.get("repo") or "власник/репозиторій"
        file = "" if (pub.get("path") or "hgs-server.json") == "hgs-server.json" else "/" + pub["path"]
        self.game_line.setText(f'HGS.start({{ gameId: "...", server: "github:{repo}{file}" }})')
        self.public_ip.setText(data["public_ip"] or (data["public_ip_error"] or "—"))
        if data["cgnat_suspected"]:
            self.cgnat.setText("⚠ Схоже на CGNAT провайдера: пряме підключення не працюватиме — використовуйте тунель.")
        else:
            self.cgnat.setText("Якщо IP у роутері відрізняється від публічної, у провайдера CGNAT — тоді лише тунель.")
        self.lan.setText(", ".join(data["lan_ips"]) or "—")
        if data.get("last_check"):
            self._show_check(data["last_check"])

    def _provider_changed(self) -> None:
        provider = self.provider.currentData()
        named = provider == "cloudflare_named"
        ts = provider == "tailscale_funnel"
        self.named_url.setEnabled(named)
        self.token_btn.setEnabled(named)
        self.prog_label.setText("tailscale:" if ts else "cloudflared:")
        self.cf_path.setVisible(not ts)
        self.download_btn.setVisible(not ts)
        self.ts_path.setVisible(ts)
        self.install_ts.setVisible(ts)

    # -- actions ------------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any, title: str) -> None:
        def done(ok: bool, data: Any) -> None:
            if ok:
                self.banner.hide()
            else:
                self.banner.show_message(f"{title}: {data}")
            self.refresh()
        self.client.call(method, path, body, done)

    def collect(self) -> dict:
        assert self.view is not None
        s = copy.deepcopy(self.view["settings"])
        s["mode"] = next(k for k, b in self.mode_buttons.items() if b.isChecked())
        s["tunnel"].update(provider=self.provider.currentData(), named_public_url=self.named_url.text().strip() or None,
                           cloudflared_path=self.cf_path.text().strip() or None,
                           tailscale_path=self.ts_path.text().strip() or None)
        s["direct"].update(public_host=self.public_host.text().strip() or None,
                           public_port=self.public_port.value() or None,
                           tls_cert=self.cert.text().strip() or None, tls_key=self.key.text().strip() or None,
                           duckdns_domain=self.duck_domain.text().strip().lower() or None)
        s["publish"] = {"enabled": self.pub_enabled.isChecked(), "repo": self.pub_repo.text().strip() or None,
                        "path": self.pub_path.text().strip() or "hgs-server.json"}
        return s

    def apply(self) -> None:
        if self.view is None:
            return
        new = self.collect()
        if new["mode"] in ("tunnel", "direct") and self.view["settings"]["mode"] in ("local", "lan"):
            if QMessageBox.question(
                self, "Доступ з інтернету",
                "Сервер стане доступним з інтернету. Вхід без пароля (режим open) буде вимкнено; використовуйте "
                "коди запрошень, пароль гри або акаунти.\n\nПродовжити?") != QMessageBox.Yes:
                return
        if QMessageBox.question(self, "Застосувати", "Підключені гравці буде на мить відключено (SDK під'єднається "
                                "знову автоматично). Продовжити?") != QMessageBox.Yes:
            return
        body: dict[str, Any] = {"network": new}
        if self.port.value() != self.view["gateway_port"]:
            body["gateway_port"] = self.port.value()
        self._save(body)

    def _save(self, body: dict, then: Any = None) -> None:
        def done(ok: bool, data: Any) -> None:
            if ok:
                self.dirty = False
                self.apply_btn.setText("Застосувати")
                self.banner.hide()
                if then:
                    then()
            else:
                self.banner.show_message(f"Мережеві налаштування: {data}")
            self.refresh()
        self.client.call("PUT", "/network", body, done)

    def save_and_publish(self) -> None:
        if self.view is None:
            return
        repo = self.pub_repo.text().strip()
        if "/" not in repo:
            self.pub_state.setText("✗ впишіть репозиторій у форматі логін/назва, наприклад idologonia/hgs-address")
            self.pub_state.setStyleSheet(f"color: {RED};")
            return
        self.pub_enabled.setChecked(True)
        # save only the publishing part; other unsaved changes stay as they are in the form
        current = copy.deepcopy(self.view["settings"])
        current["publish"] = {"enabled": True, "repo": repo, "path": self.pub_path.text().strip() or "hgs-server.json"}
        self._save({"network": current},
                   then=lambda: self._call("POST", "/network/publish", None, "Публікація адреси"))

    def _set_secret(self, name: str, title: str) -> None:
        value, ok = QInputDialog.getText(self, title, f"{title} (зберігається зашифрованим):", QLineEdit.Password)
        if ok and value.strip():
            self._call("PUT", f"/secrets/{name}", {"value": value.strip()}, title)

    def _browse_cloudflared(self) -> None:
        ts = self.provider.currentData() == "tailscale_funnel"
        name = "tailscale" if ts else "cloudflared"
        path, _ = QFileDialog.getOpenFileName(self, f"Виберіть {name}", "", f"{name} ({name}*);;Усі файли (*)")
        if path:
            (self.ts_path if ts else self.cf_path).setText(path)
            self._mark_dirty()

    def _browse_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Виберіть файл", "", "PEM (*.pem *.crt *.key);;Усі файли (*)")
        if path:
            edit.setText(path)

    def _download(self) -> None:
        if QMessageBox.question(
            self, "Завантажити cloudflared",
            "HGS завантажить офіційну програму cloudflared з GitHub-репозиторію Cloudflare "
            "(github.com/cloudflare/cloudflared), перевірить контрольну суму SHA-256 і збереже її в папці даних HGS.\n\n"
            "Продовжити?") != QMessageBox.Yes:
            return
        self.download_btn.setEnabled(False)
        self.download_btn.setText("Завантаження…")

        def done(ok: bool, data: Any) -> None:
            self.download_btn.setEnabled(True)
            self.download_btn.setText("Завантажити cloudflared")
            if ok:
                QMessageBox.information(self, "cloudflared", f"Завантажено {data['version']} і перевірено.\n{data['path']}")
            else:
                self.banner.show_message(f"Завантаження cloudflared: {data}")
            self.refresh()
        self.client.call("POST", "/network/cloudflared/download", None, done)

    def _check(self) -> None:
        self.check_btn.setEnabled(False)
        self.check_result.setText("Перевірка…")

        def done(ok: bool, data: Any) -> None:
            self.check_btn.setEnabled(True)
            if ok:
                self._show_check(data)
            else:
                self.check_result.setText(f"Помилка: {data}")
        self.client.call("POST", "/network/check", None, done)

    def _show_check(self, r: dict) -> None:
        mark, color = ("✓", GREEN) if r["ok"] else ("✗", RED)
        text = f"{mark} {r.get('detail', '')}"
        if r.get("latency_ms") is not None:
            text += f" ({r['latency_ms']} мс)"
        if r.get("hint"):
            text += "\n" + r["hint"]
        self.check_result.setText(text)
        self.check_result.setStyleSheet(f"color: {color};")
