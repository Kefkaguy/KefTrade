export function money(value: unknown) {
  const number = Number(value ?? 0);
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(number);
}

export function percent(value: unknown) {
  const number = Number(value ?? 0);
  return `${(number * 100).toFixed(2)}%`;
}

export function number(value: unknown, digits = 2) {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "N/A";
}

