import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn's standard class merger: clsx for conditionals, tailwind-merge to
 * resolve conflicting Tailwind utilities so the last one genuinely wins. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
