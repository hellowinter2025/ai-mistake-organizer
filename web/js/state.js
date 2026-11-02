// 全局状态与事件总线
export const state = {
  token: '',
  version: '',
  settings: {},
  active: null,
  stats: {},
  images: [],          // {id, url, thumb, width, height, name, candidates, ...}
  currentImageId: null,
  boxesByImage: {},    // imageId -> [{id,x,y,w,h,source,selected,wrong_score,mark_count,mark_kinds}]
  queue: [],           // {id, imageId, bbox, status, fields, meta, crop, error}
  bank: { items: [], total: 0, keyword: '', starred: false, subject: '' },
  paper: { items: [], saved: [] },
  cart: JSON.parse(localStorage.getItem('mm_cart') || '[]'),
  editing: null,
};

const bus = new EventTarget();
export function emit(type, detail) { bus.dispatchEvent(new CustomEvent(type, { detail })); }
export function on(type, fn) { bus.addEventListener(type, fn); }

export function saveCart() {
  localStorage.setItem('mm_cart', JSON.stringify(state.cart));
  emit('cart:changed');
}

export function currentBoxes() {
  return state.boxesByImage[state.currentImageId] || [];
}

export function setBoxes(imageId, boxes) {
  state.boxesByImage[imageId] = boxes;
  emit('boxes:changed', { imageId });
}

export function newId() {
  return 'b' + Math.random().toString(36).slice(2, 10);
}
