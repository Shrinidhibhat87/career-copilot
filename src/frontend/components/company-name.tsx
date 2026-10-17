/** The company under a role title: a link in a new tab when its site is known, plain
 * text otherwise. */
export function CompanyName({
  name,
  url,
}: {
  name: string;
  url: string | null;
}) {
  return url ? (
    <a
      className="company-name"
      href={url}
      rel="noopener noreferrer"
      target="_blank"
    >
      {name}
    </a>
  ) : (
    <span className="company-name">{name}</span>
  );
}
