import React from "react";

// Planted: renders user-supplied HTML without sanitising it.
export function Comment({ comment }) {
  return <div className="comment" dangerouslySetInnerHTML={{ __html: comment.body }} />;
}
