import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/theme.css";
import { initToken } from "./api/client";
import { App } from "./App";
import { TerminalProvider } from "./state/store";

initToken();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TerminalProvider>
      <App />
    </TerminalProvider>
  </StrictMode>,
);
