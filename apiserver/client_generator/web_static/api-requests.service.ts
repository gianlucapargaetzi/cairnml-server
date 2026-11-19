import {Injectable} from '@angular/core';
import {HTTP} from '../../app.constants';
import {Observable} from 'rxjs';
import {IApiRequest, SmHttpResponse} from '../model/api-request';
import {HttpClient, HttpParams} from '@angular/common/http';
import {HttpHeaders} from '@angular/common/http';
import {map} from 'rxjs/operators';
import {ApiOptions} from './api';

/**
 * The options of one call that the service takes: the headers and the query of the HTTP request. The options of the
 * call (ApiOptions, the last argument of a generated method) are merged into them: its headers are added, its
 * parameters are appended to the query.
 */
export interface CallOptions {
  headers?: HttpHeaders | Record<string, string | string[]>;
  observe?: 'body';
  params?: HttpParams | Record<string, string | string[]>;
  reportProgress?: boolean;
  responseType?: 'json';
  withCredentials?: boolean;
}

@Injectable({providedIn: 'root'})
export class SmApiRequestsService {

  constructor(private http: HttpClient) {
  }

  createRequest(request: IApiRequest): Observable<any> {

    return this.http.request(
      request.meta.method,
      request.meta.endpoint[0] === '/' ? request.meta.endpoint : `${HTTP.API_BASE_URL}/${request.meta.endpoint}`,
      {
        params         : this.getParams(request.params),
        body           : request.payload,
        withCredentials: true
      });
  }

  post<T>(url: string, body: any | null, options?: CallOptions, extOptions?: ApiOptions): Observable<T> {
    return this.http.post<SmHttpResponse>(url, body, this.withOptions(options, extOptions))
      .pipe(map(res => res.data));
  }

  /** A POST whose response is a file: the response is the file itself, not the JSON envelope. */
  postBlob(url: string, body: any | null, options?: CallOptions, extOptions?: ApiOptions): Observable<Blob> {
    return this.http.post(url, body, {...this.withOptions(options, extOptions), responseType: 'blob'});
  }

  /** A GET: the query is the request's fields (a scalar, or a list of values), then the parameters of the options. */
  get<T>(url: string, query: object | null, options?: CallOptions, extOptions?: ApiOptions): Observable<T> {
    return this.http.get<SmHttpResponse>(url, this.withOptions(options, extOptions, query))
      .pipe(map(res => res.data));
  }

  /** A GET whose response is a file. */
  getBlob(url: string, query: object | null, options?: CallOptions, extOptions?: ApiOptions): Observable<Blob> {
    return this.http.get(url, {...this.withOptions(options, extOptions, query), responseType: 'blob'});
  }

  /** The address of a call with the fields of the request as its query: a link the browser follows. */
  url(address: string, query: object | null): string {
    const text = this.toParams(query).toString();
    return text ? `${address}?${text}` : address;
  }

  posti(url: string, body: any | null, options?: CallOptions): Observable<SmHttpResponse> {
    return this.http.post<SmHttpResponse>(url, body, this.withOptions(options, undefined));
  }

  private withOptions(options: CallOptions | undefined, extOptions: ApiOptions | undefined, query?: object | null): {
    headers: HttpHeaders;
    params: HttpParams;
    withCredentials: true;
  } {
    let headers = options?.headers instanceof HttpHeaders
      ? options.headers
      : new HttpHeaders(options?.headers as Record<string, string | string[]>);
    for (const [name, value] of Object.entries(extOptions?.headers ?? {})) {
      headers = headers.set(name, value);
    }
    return {
      headers,
      params         : this.toParams(options?.params, query, extOptions?.params),
      withCredentials: true
    };
  }

  private toParams(...sources: Array<HttpParams | object | null | undefined>): HttpParams {
    let params = new HttpParams();
    for (const source of sources) {
      if (source instanceof HttpParams) {
        for (const key of source.keys()) {
          for (const value of source.getAll(key) ?? []) {
            params = params.append(key, value);
          }
        }
        continue;
      }
      for (const [key, value] of Object.entries(source ?? {})) {
        if (value === undefined || value === null) {
          continue;
        }
        for (const item of Array.isArray(value) ? value : [value]) {
          params = params.append(key, String(item));
        }
      }
    }
    return params;
  }

  private getParams(actionParams) {
    let params: HttpParams = new HttpParams();

    if (actionParams) {
      Object.keys(actionParams)
        .forEach(key => params = params.append(key, actionParams[key]));
    }

    return params;
  }

}
