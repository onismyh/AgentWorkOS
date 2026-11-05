// Tiny indirection so low-level modules can raise a toast without importing the overlay implementation (avoids cycles).
let sink = null;
export function setToastSink(fn) { sink = fn; }
export function toastError(err) {
  const msg = (err && err.message) || String(err);
  if (sink) sink({ msg, kind: 'err' }); else console.error(msg);
}
