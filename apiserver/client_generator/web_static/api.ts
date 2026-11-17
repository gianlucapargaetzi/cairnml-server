/**
 * Options of one call: the headers and query flags the web app adds to a request. Every generated service takes them.
 */
export interface ApiOptions {
  headers?: Record<string, string>;
  adminQuery?: boolean;
  userId?: string;
}
