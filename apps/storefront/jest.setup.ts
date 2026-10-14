// Registers the DOM matchers on Jest's expect. Both entry points: the default one types the global `expect`,
// the jest-globals one types `import { expect } from "@jest/globals"`; at runtime they extend the same object.
import "@testing-library/jest-dom";
import "@testing-library/jest-dom/jest-globals";
