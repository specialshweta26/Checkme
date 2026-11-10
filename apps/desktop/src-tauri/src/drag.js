(function (from, to) {
  // Pages that use the browser's own drag and drop ignore mouse events sent through the debugging port. Send that page
  // the drag events themselves, with one shared DataTransfer, from the dragged element onto the target.
  const find = (ref) => document.querySelector('[data-checkme-ref="' + ref + '"]');
  const a = find(from);
  const b = find(to);
  if (!a || !b) return { html5: false };
  const src = a.closest('[draggable="true"]');
  if (!src) return { html5: false };
  const data = new DataTransfer();
  const fire = (el, type) => {
    const r = el.getBoundingClientRect();
    el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: data, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 }));
  };
  fire(src, 'dragstart');
  fire(b, 'dragenter');
  fire(b, 'dragover');
  fire(b, 'drop');
  fire(src, 'dragend');
  return { html5: true };
})
