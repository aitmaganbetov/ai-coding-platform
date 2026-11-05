import React from "react";
import { createRoot } from "react-dom/client";
import { Comment } from "./components/Comment";

createRoot(document.getElementById("app")).render(<Comment comment={{ body: "hello" }} />);
