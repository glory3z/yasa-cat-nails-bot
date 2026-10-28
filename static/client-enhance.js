(function () {
  'use strict';

  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  let config = null;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        ...(options.headers || {}),
        'X-Telegram-Init-Data': initData,
      },
    });
    if (!response.ok) {
      let message = 'Не удалось выполнить действие.';
      try {
        const data = await response.json();
        if (data.detail) message = data.detail;
      } catch (error) {}
      throw new Error(message);
    }
    return response.json();
  }

  function money(value) {
    return Number(value || 0).toLocaleString('ru-RU') + ' ₽';
  }

  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (char) {
      return {'&':'&','<':'<','>':'>','"':'"',"'":'&#39;'}[char];
    });
  }

  function injectCommentField() {
    const form = document.getElementById('booking-form');
    if (!form || document.getElementById('client-comment')) return;
    const label = document.createElement('label');
    label.className = 'label';
    label.htmlFor = 'client-comment';
    label.textContent = 'Комментарий мастеру';
    const textarea = document.createElement('textarea');
    textarea.id = 'client-comment';
    textarea.rows = 3;
    textarea.maxLength = 2000;
    textarea.style.cssText = 'width:100%;border:1px solid var(--line);border-radius:12px;padding:14px;font:inherit;resize:vertical;';
    const submitButton = form.querySelector('button[type="submit"]');
    form.insertBefore(label, submitButton);
    form.insertBefore(textarea, submitButton);
  }

  function ensurePaymentScreen() {
    let section = document.getElementById('payment-screen');
    if (section) return section;
    section = document.createElement('section');
    section.id = 'payment-screen';
    section.className = 'screen';
    section.hidden = true;
    section.innerHTML = `
      <button class="back" id="payment-back">← Изменить запись</button>
      <h1>Предоплата</h1>
      <div id="payment-summary" class="summary"></div>
      <div class="info-banner">
        <b>Реквизиты для перевода:</b><br>
        <span id="payment-phone"></span><br>
        <span id="payment-bank"></span><br>
        Предоплата: <b id="payment-deposit"></b>
      </div>
      <label class="label" for="receipt-file">Чек об оплате</label>
      <input id="receipt-file" type="file" accept=".jpg,.jpeg,.png,.webp,.pdf">
      <label class="label" for="receipt-comment">Комментарий к чеку</label>
      <textarea id="receipt-comment" rows="3" style="width:100%;border:1px solid var(--line);border-radius:12px;padding:14px;font:inherit;resize:vertical;"></textarea>
      <button id="receipt-submit" class="primary" style="margin-top:20px" disabled>Отправить чек</button>
      <p id="receipt-error" class="error" role="alert"></p>
    `;
    document.querySelector('main').appendChild(section);
    return section;
  }

  function showPaymentScreen(booking) {
    const section = ensurePaymentScreen();
    const addons = booking.addons || [];
    section.querySelector('#payment-summary').innerHTML = `
      <p class="service-name">${escapeText(booking.service_name)}</p>
      ${addons.length ? `<div class="summary-row"><span>Допы</span><span>${addons.map(function (addon) { return escapeText(addon.name); }).join('<br>')}</span></div>` : ''}
      <div class="summary-row"><span>Дата и время</span><span>${new Date(booking.starts_at).toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })}</span></div>
      <div class="summary-row"><span>Итого</span><span>${money(booking.total_rub)}</span></div>
      <div class="summary-row total"><span>Предоплата</span><span>${money(booking.deposit_rub)}</span></div>
    `;
    section.querySelector('#payment-phone').textContent = config?.settings?.payment_phone || '';
    section.querySelector('#payment-bank').textContent = config?.settings?.payment_bank || '';
    section.querySelector('#payment-deposit').textContent = money(booking.deposit_rub);
    section.querySelector('#receipt-file').value = '';
    section.querySelector('#receipt-comment').value = '';
    section.querySelector('#receipt-submit').disabled = true;
    section.querySelector('#receipt-error').textContent = '';
    document.querySelectorAll('.screen').forEach(function (screen) {
      screen.hidden = screen.id !== 'payment-screen';
    });
    window.scrollTo(0, 0);
  }

  document.addEventListener('submit', async function (event) {
    const form = event.target;
    if (form.id !== 'booking-form') return;
    event.preventDefault();
    event.stopPropagation();
    if (typeof state === 'undefined' || !state.service || !state.date || !state.time) return;

    const name = document.getElementById('name').value.trim();
    const telegramUsername = document.getElementById('tg-user').value.trim();
    const comment = document.getElementById('client-comment')?.value.trim() || '';
    const errorElement = document.getElementById('form-error');
    if (!name) {
      errorElement.textContent = 'Укажи имя.';
      return;
    }

    const submitButton = form.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    try {
      const booking = await api('/api/bookings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service_id: state.service.id,
          addon_ids: state.addons.map(function (addon) { return addon.id; }),
          date: state.date,
          time: state.time,
          name: name,
          tg: telegramUsername,
          comment: comment,
        }),
      });
      window.currentBooking = booking;
      showPaymentScreen(booking);
    } catch (error) {
      errorElement.textContent = error.message;
    } finally {
      submitButton.disabled = false;
    }
  }, true);

  document.addEventListener('change', function (event) {
    if (event.target.id !== 'receipt-file') return;
    const submitButton = document.getElementById('receipt-submit');
    submitButton.disabled = !event.target.files?.length;
  });

  document.addEventListener('click', async function (event) {
    if (event.target.id === 'payment-back') {
      document.getElementById('payment-screen').hidden = true;
      window.go('services');
      return;
    }
    if (event.target.id !== 'receipt-submit') return;

    const booking = window.currentBooking;
    const fileInput = document.getElementById('receipt-file');
    const file = fileInput.files?.[0];
    if (!booking || !file) return;

    const button = event.target;
    button.disabled = true;
    button.textContent = 'Отправляю...';
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('comment', document.getElementById('receipt-comment').value.trim());
      const response = await fetch(`/api/bookings/${booking.id}/receipt`, {
        method: 'POST',
        body: formData,
        headers: { 'X-Telegram-Init-Data': initData },
      });
      if (!response.ok) {
        let message = 'Не удалось отправить чек.';
        try {
          const data = await response.json();
          if (data.detail) message = data.detail;
        } catch (error) {}
        throw new Error(message);
      }
      document.getElementById('success-summary').innerHTML =
        document.getElementById('payment-summary').innerHTML +
        '<div class="summary-row"><span>Чек</span><span>Отправлен на проверку</span></div>';
      document.getElementById('payment-screen').hidden = true;
      window.go('success');
    } catch (error) {
      document.getElementById('receipt-error').textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = 'Отправить чек';
    }
  });

  const STATUS_TEXT = {
    awaiting_receipt: 'Ждёт чек',
    pending_review: 'Чек на проверке',
    payment_not_found: 'Оплата не найдена',
    confirmed: 'Подтверждена',
    declined: 'Отклонена',
    cancelled: 'Отменена',
    reschedule_requested: 'Согласуется перенос',
    cancel_requested: 'Запрошена отмена',
  };

  async function renderMyBookings() {
    const container = document.getElementById('bookings-content');
    if (!container) return;
    try {
      const bookings = await api('/api/bookings/mine');
      if (!bookings.length) {
        container.innerHTML = '<div class="empty"><h2>Пока нет записей</h2><p>Выбери услугу и удобное время.</p><button class="primary" data-go="services">Выбрать услугу</button></div>';
        return;
      }
      container.innerHTML = bookings.map(function (booking) {
        const canCancel = ['awaiting_receipt', 'pending_review'].includes(booking.status);
        return `
          <div class="summary" style="margin-bottom:14px">
            <p class="service-name">${escapeText(booking.service_name)}</p>
            <div class="summary-row"><span>Дата и время</span><span>${new Date(booking.starts_at).toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })}</span></div>
            <div class="summary-row"><span>Стоимость</span><span>${money(booking.total_rub)}</span></div>
            <div class="summary-row"><span>Предоплата</span><span>${money(booking.deposit_rub)}</span></div>
            <div class="summary-row"><span>Статус</span><span>${STATUS_TEXT[booking.status] || escapeText(booking.status)}</span></div>
            ${canCancel ? `<div class="booking-actions"><button class="secondary" data-cancel="${booking.id}">Отменить</button></div>` : ''}
          </div>
        `;
      }).join('');
      container.querySelectorAll('[data-cancel]').forEach(function (button) {
        button.onclick = async function () {
          if (!window.confirm('Отменить запись?')) return;
          await api(`/api/bookings/${button.dataset.cancel}/cancel`, { method: 'POST' });
          renderMyBookings();
        };
      });
    } catch (error) {
      container.innerHTML = `<div class="empty"><h2>Не удалось загрузить</h2><p>${escapeText(error.message)}</p></div>`;
    }
  }

  document.addEventListener('click', function (event) {
    const trigger = event.target.closest('[data-nav="bookings"],[data-go="bookings"]');
    if (trigger) setTimeout(renderMyBookings, 50);
  }, true);

  function addAdminButton() {
    const homeScreen = document.getElementById('home');
    if (!homeScreen || document.getElementById('admin-open')) return;
    const button = document.createElement('button');
    button.id = 'admin-open';
    button.className = 'secondary';
    button.style.marginTop = '10px';
    button.textContent = 'Кабинет мастера';
    button.onclick = function () {
      window.location.href = '/admin';
    };
    homeScreen.appendChild(button);
  }

  (async function init() {
    try {
      config = await api('/api/config');
    } catch (error) {
      console.error(error);
    }
    injectCommentField();
    addAdminButton();
  })();
})();
