import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import "./index.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error('index.html is missing its <div id="root"> mount point.');
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
