// Local bidi handling: the shell stays LTR; a piece of text that contains Hebrew renders in its own direction.
const HEBREW = /[֐-׿]/;
const STRONG_LTR = /[A-Za-z]/;

export function hasHebrew(text: string): boolean {
  return HEBREW.test(text);
}

/** "rtl" when the text's first strong character is Hebrew, "ltr" otherwise (ids, URLs, numbers stay LTR). */
export function textDirection(text: string): "rtl" | "ltr" {
  for (const char of text) {
    if (HEBREW.test(char)) return "rtl";
    if (STRONG_LTR.test(char)) return "ltr";
  }
  return "ltr";
}
