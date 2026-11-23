'use strict';

const $ = id => document.getElementById(id);
const names = {router: 'Роутер', esp1: 'ESP 1', esp2: 'ESP 2'};
const colors = {train: '#6fe5d1', test: '#ffc16c'};
let ws, state, pending = null, tableKey = '', nextRequest = 0, trainingRequest = null;
let trainFromPlan = new URLSearchParams(location.search).get('train');

function send(action, fields = {}) {
 const status = action === 'status';
 if (ws?.readyState !== WebSocket.OPEN || (!status && !state)) {
  if (!status) $('error').textContent = 'Нет связи с локальным сервером.';
  return false;
 }
 if (!status) $('error').textContent = '';
 ws.send(JSON.stringify({cmd:'ranging_web_' + action, dataset_id:state?.dataset.id, ...fields}));
 return true;
}

function updateTrainingButtons() {
 const busy = !state || !!trainingRequest || !!state.recording || state.busy_elsewhere || ws?.readyState !== WebSocket.OPEN;
 $('train').disabled = busy || !state?.can_train;
 $('validate').disabled = busy || !state?.can_validate;
}

function finishTraining(message) {
 clearTimeout(trainingRequest.timer);
 trainingRequest = null;
 $('trainingAction').textContent = message;
 updateTrainingButtons();
}

function runTraining(action, fields = {}) {
 if (trainingRequest) return;
 const id = ++nextRequest;
 if (!send(action, {...fields, request_id:id})) return;
 $('trainingAction').textContent = action === 'train' ? 'Обучаю по сохранённым записям…' : 'Проверяю сохранённые записи…';
 trainingRequest = {id, timer:setTimeout(() => finishTraining('Сервер не подтвердил результат. Попробуйте ещё раз.'), 12000)};
 updateTrainingButtons();
}

function record(id) {
 const delay = $('delay'), seconds = $('seconds');
 if (!delay.reportValidity() || !seconds.reportValidity()) return;
 send('record', {id, delay:delay.valueAsNumber, seconds:seconds.valueAsNumber});
}

function labels(points) {
 const counts = {train:0, test:0};
 return points.map(point => ({...point, label:(point.role === 'train' ? 'О' : 'П') + ++counts[point.role]}));
}

function marker(tag, attrs, text) {
 const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
 for (const [key,value] of Object.entries(attrs)) node.setAttribute(key, value);
 if (text) node.textContent = text;
 $('map').appendChild(node);
}

function draw() {
 const map = $('map'), room = state?.room;
 map.replaceChildren();
 if (!room) return;
 const [left, top, right, bottom] = RoomPlan.bounds(room);
 const size = Math.max(right-left, bottom-top);
 const pad = size * .07, radius = size * .011;
 map.setAttribute('viewBox', [left-pad, top-pad, right-left+2*pad, bottom-top+2*pad].join(' '));
 const outline = room.shape === 'custom' ? RoomPlan.path(room)
  : room.shape === 'curved' ? `M0 0H${room.width} A${room.width-room.short_width} ${room.depth} 0 0 1 ${room.short_width} ${room.depth} H0Z`
  : `M0 0H${room.width}V${room.depth}H0Z`;
 marker('path', {d:outline, fill:'#132b40', stroke:'#53708b', 'stroke-width':radius*.3, 'fill-rule':'evenodd'});
 function place(position, color, label) {
  const [x,z] = position;
  marker('circle', {cx:x, cy:z, r:radius, fill:color, stroke:'#091521', 'stroke-width':radius*.22});
  if (label) marker('text', {x:x+radius*1.35, y:z-radius*1.15, fill:color, 'font-size':radius*1.7,
   stroke:'#091521', 'stroke-width':radius*.34, 'paint-order':'stroke'}, label);
 }
 for (const [key,device] of Object.entries(state.devices)) place(device.position, '#c299ff', names[key]);
 for (const point of labels(state.points)) place(point.position, colors[point.role], point.label);
 if (pending) place(pending, '#fff', '');
 if (state.estimate) place(state.estimate.position, '#69baff', 'Оценка');
}

function availableToRecord(s) {
 return ws?.readyState === WebSocket.OPEN && s.signal_ready && !s.recording && !s.busy_elsewhere;
}

function render(s) {
 if (s.dataset.id !== state?.dataset.id) {
  pending = null; $('x').value = ''; $('z').value = '';
  if (!trainingRequest) $('trainingAction').textContent = '';
 }
 if (pending && s.points.some(point => point.position[0] === pending[0] && point.position[1] === pending[1])) pending = null;
 state = s;
 $('dataset').textContent = s.room ? s.dataset.name : 'Сначала сохраните план комнаты на странице локализации.';
 $('radio').textContent = s.signal_ready ? `Радиолиний: ${s.lines.length}. Можно записывать.`
  : 'Ожидание пригодного сигнала двух ESP. Точки и сохранённые записи доступны.';
 $('message').textContent = s.message;
 const rec = s.recording, canRecord = availableToRecord(s);
 const busy = !!rec || s.busy_elsewhere;
 $('add').disabled = !s.room || busy;
 $('background').disabled = $('empty_check').disabled = !canRecord || !s.room;
 $('cancel').disabled = !rec;
 updateTrainingButtons();
 const training = s.training;
 $('trainingStatus').textContent = `Обучающих точек: ${training.points} (нужно минимум ${training.required}). Пригодных записей: ${training.recorded}/${training.points}. Фон: ${training.background ? 'готов' : 'нужна запись'}.` +
  (training.points < training.required && s.points.some(point => point.role === 'test')
   ? ' Точку проверки можно перенести в обучение.' : '');
 for (const [id,title] of [['background','Записать пустую комнату'],['empty_check','Записать пустую комнату для проверки']]) {
  $(id).textContent = title + (s.windows[id] ? ` · ${s.windows[id]} окон` : s.stale.includes(id) ? ' · нужна новая запись' : '');
 }
 $('recordState').textContent = rec ? (rec.phase === 'waiting' ? `До начала ${rec.left_s} с`
  : `Запись: ${rec.left_s} с · ${rec.windows} пригодных окон`) : s.busy_elsewhere ? 'Идёт запись на странице локализации.' : '';
 const key = JSON.stringify([s.points, s.windows, s.stale, canRecord, busy]);
 if (key !== tableKey) {
  tableKey = key;
  $('points').replaceChildren();
  for (const point of labels(s.points)) {
   const row = document.createElement('tr');
   for (const text of [point.label, point.position.map(v => String(v)).join(' / '),
    s.windows[point.id] ? `${s.windows[point.id]} окон` : s.stale.includes(point.id) ? 'Нужна новая запись' : '—']) {
    const cell = document.createElement('td');
    cell.textContent = text; row.appendChild(cell);
   }
   const actions = document.createElement('td'), group = document.createElement('div');
   group.className = 'row';
   function button(text, title, disabled, action) {
    const control = document.createElement('button');
    control.textContent = text; control.title = title; control.disabled = disabled; control.onclick = action;
    group.appendChild(control);
   }
   button('Записать', 'Записать точку ' + point.label, !canRecord, () => record(point.id));
   if (point.role === 'test') button('В обучение', 'Перенести точку ' + point.label + ' в обучение', busy, () => {
    if (window.confirm(`Перенести точку ${point.label} в обучение вместе с её записью?`)) send('promote', {id:point.id});
   });
   button('Удалить', 'Удалить точку ' + point.label, busy, () => {
    const hasRecording = s.windows[point.id] || s.stale.includes(point.id);
    if (!hasRecording || window.confirm(`Удалить точку ${point.label} и её запись?`)) send('delete', {id:point.id});
   });
   actions.appendChild(group); row.appendChild(actions); $('points').appendChild(row);
  }
 }
 const report = s.report;
 const diagnostic = s.recording_check;
 $('recordingCheck').hidden = !diagnostic;
 $('recordingRows').replaceChildren();
 if (diagnostic) {
  for (const [index, row] of diagnostic.training.entries()) {
   const tr = document.createElement('tr');
   const heldOut = diagnostic.held_out?.[index];
   for (const value of [row.position.join(' / '), `${row.matched}/${row.windows}`,
    heldOut ? `${heldOut.matched}/${heldOut.windows}` : '—',
    row.distance_mm ? ['router','esp1','esp2'].map(id => row.distance_mm[id]).join(' / ') : '—']) {
    const td = document.createElement('td');
    td.textContent = value; tr.appendChild(td);
   }
   $('recordingRows').appendChild(tr);
  }
  const check = diagnostic.empty;
  $('emptyResult').textContent = check ? `Контрольная пустая комната: ${check.false_positions}/${check.windows} ложных оценок.`
   : 'Запишите пустую комнату для проверки ложных срабатываний.';
 }
 const absent = 'Для оценки погрешности запустите независимую проверку.';
 $('report').textContent = report ? (report.passes_at_control_points ? 'Допуск пройден в контрольных точках. '
  : 'Допуск не пройден в контрольных точках. ') +
  'P90: ' + Object.entries(report.p90_error_mm).map(([id, value]) => names[id] + ' ' + (value == null ? '—' : value + ' мм')).join(' · ') +
  `. Оценок: ${report.estimated_windows}/${report.windows}. Ложные оценки в пустой комнате: ${report.empty_false_positions}/${report.empty_windows}.`
  : s.model_ready ? absent : 'Модель пока не обучена.';
 $('result').textContent = s.estimate ? Object.entries(s.estimate.distance_mm).map(([id,value]) => names[id]+' '+value+' мм').join(' · ')
  : !s.model_ready ? 'Нужна обученная модель'
  : s.background_failed ? 'Оценки скрыты: модель дала ложные срабатывания на контрольной пустой комнате'
  : !s.signal_ready ? 'Модель готова. Для оценки в реальном времени нужен сигнал от двух ESP'
  : s.live_state === 'lines_changed' ? 'Радиолинии отличаются от записи обучения. Проверьте адреса ESP и канал роутера.'
  : 'Пусто или текущий сигнал не похож на записи обучения';
 draw();
}

$('map').addEventListener('click', event => {
 if (!state?.room || state.recording) return;
 const map = $('map'), matrix = map.getScreenCTM();
 if (!matrix) return;
 const svgPoint = map.createSVGPoint();
 svgPoint.x = event.clientX; svgPoint.y = event.clientY;
 const point = svgPoint.matrixTransform(matrix.inverse());
 pending = [point.x,point.y].map(v => Math.round(v*1000)/1000);
 $('x').value = pending[0]; $('z').value = pending[1]; draw();
});
$('pointForm').onsubmit = event => {
 event.preventDefault();
 send('add',{role:$('role').value, x:$('x').valueAsNumber, z:$('z').valueAsNumber});
};
$('background').onclick = () => record('background');
$('empty_check').onclick = () => record('empty_check');
$('cancel').onclick = () => send('cancel');
$('train').onclick = () => runTraining('train');
$('validate').onclick = () => {
 const input = $('tolerance');
 if (input.reportValidity()) runTraining('validate', {tolerance_mm:input.valueAsNumber});
};

function connect() {
 ws = new WebSocket('ws://' + (location.hostname || '127.0.0.1') + ':8765/ranging');
 ws.onopen = () => {
  $('connection').textContent = 'Локальный сервер подключён';
  $('error').textContent = '';
  send('status');
 };
 ws.onmessage = event => {
  const message = JSON.parse(event.data);
  if (trainingRequest && message.request_id === trainingRequest.id) finishTraining(message.message);
  else if (message.type === 'ranging_web_error') $('error').textContent = message.message;
  if (message.type === 'ranging_web') render(message);
  if (message.type === 'ranging_web' && trainFromPlan) {
   const dataset = trainFromPlan;
   trainFromPlan = null;
   history.replaceState(null, '', location.pathname);
   if (dataset === state.dataset.id && !$('train').disabled) runTraining('train');
   else $('trainingAction').textContent = 'Проверьте набор и готовность обучающих записей.';
  }
 };
 ws.onclose = () => {
  $('connection').textContent = 'Локальный сервер недоступен. Переподключение…';
  if (trainingRequest) finishTraining('Связь с сервером прервалась. Повторите действие после подключения.');
  for (const id of ['add','background','empty_check','cancel','train','validate']) $(id).disabled = true;
  for (const button of $('points').querySelectorAll('button')) button.disabled = true;
  if (state) { state.estimate = null; draw(); }
  $('result').textContent = 'Нет связи с локальным сервером';
  tableKey = '';
  setTimeout(connect, 1800);
 };
}

connect();
setInterval(() => send('status'), 1000);
