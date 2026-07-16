import React from "react";
import { createRoot } from "react-dom/client";
import DriveLikeApp from "./DriveLikeApp";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DriveLikeApp />
  </React.StrictMode>
);
