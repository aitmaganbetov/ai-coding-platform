// Positive sample: dangerouslySetInnerHTML (test fixture).
export function Html({ html }) {
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
