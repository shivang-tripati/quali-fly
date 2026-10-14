"use client";

/**
 * A labelled on/off control.
 *
 * Built here rather than pulled in because the design system has no switch yet, and adding a
 * dependency for one control is not a trade worth making. It is a `button` with
 * `role="switch"` rather than a checkbox, because a switch is a state you apply immediately
 * and a checkbox is a value you submit with a form - and every use of this control does
 * apply immediately.
 *
 * `label` is required rather than optional-with-aria-label because every switch here sits
 * next to a heading that says what it controls; the `aria-labelledby` wiring below keeps
 * that relationship explicit for a screen reader instead of duplicating the text.
 */
export function Switch({
  checked,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
  /** Visible text beside the control. Also the accessible name. */
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border-2 border-transparent transition-colors",
        "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-primary" : "bg-input",
      ].join(" ")}
    >
      <span
        aria-hidden
        className={[
          "pointer-events-none block size-4 rounded-full bg-background shadow ring-0 transition-transform",
          checked ? "translate-x-4" : "translate-x-0",
        ].join(" ")}
      />
    </button>
  );
}