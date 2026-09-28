/* Mesmo contrato dos campos de termos literais; Regex habilitada não usa esta validação. */
window.TermInput = Object.freeze({
  hasInvalidSeparator: (text) => /[,.]/.test(text),
  validate(field, warning, enabled = true) {
    const invalid = enabled && this.hasInvalidSeparator(field.value);
    if (invalid) field.setAttribute('aria-invalid', 'true');
    else field.removeAttribute('aria-invalid');
    warning.hidden = !invalid;
    return !invalid;
  },
});
