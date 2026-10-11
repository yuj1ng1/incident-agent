import Markdown from 'react-markdown';

const allowedElements = [
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
  'strong', 'em', 'code', 'pre', 'blockquote', 'hr', 'br',
];

export function InvestigationText({ text }: { text: string }) {
  // HTML remains escaped text. Images and navigable links are excluded, and
  // model headings cannot replace the workbench's page/panel heading hierarchy.
  return <div className="answer-text markdown-text"><Markdown allowedElements={allowedElements} unwrapDisallowed
    components={{ h1: 'h3', h2: 'h3', h3: 'h4', h4: 'h4', h5: 'h4', h6: 'h4' }}>{text}</Markdown></div>;
}
