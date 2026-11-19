/**
 * Options of one call: the headers and query flags the web app adds to a request, and the query parameters of a call.
 * Every generated service takes them.
 */
export interface ApiOptions {
  headers?: Record<string, string>;
  params?: Record<string, string | string[]>;
  adminQuery?: boolean;
  userId?: string;
}
