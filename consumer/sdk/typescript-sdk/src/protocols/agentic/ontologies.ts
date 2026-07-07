import { HttpClient } from '../../transport/http.js';
import type {
  DeleteOntologyResponse,
  ImportOntologyZipResponse,
  OntologyFileListResponse,
  OntologyFileResponse,
  OntologyListResponse,
  OntologySummary,
  PutOntologyFileRequest,
  PutOntologyFileResponse,
  UploadOntologyFileResponse,
} from '../../types/agentic.js';

export class AgenticOntologiesApi {
  constructor(private readonly http: HttpClient) {}

  async list(options: { signal?: AbortSignal } = {}): Promise<OntologySummary[]> {
    const response = await this.http.get<OntologyListResponse>('/agentic/ontologies', {
      signal: options.signal,
    });
    return response.ontologies;
  }

  listFiles(
    ontologyId: string,
    folder: 'operations' | 'objects',
    options: { signal?: AbortSignal } = {},
  ): Promise<OntologyFileListResponse> {
    return this.http.get<OntologyFileListResponse>(
      `/agentic/ontologies/${encodeURIComponent(ontologyId)}/files`,
      {
        query: { folder },
        signal: options.signal,
      },
    );
  }

  getFile(
    ontologyId: string,
    folder: 'operations' | 'objects',
    name: string,
    options: { signal?: AbortSignal } = {},
  ): Promise<OntologyFileResponse> {
    return this.http.get<OntologyFileResponse>(
      `/agentic/ontologies/${encodeURIComponent(ontologyId)}/file`,
      {
        query: { folder, name },
        signal: options.signal,
      },
    );
  }

  putFile(
    ontologyId: string,
    request: PutOntologyFileRequest,
    options: { signal?: AbortSignal } = {},
  ): Promise<PutOntologyFileResponse> {
    return this.http.put<PutOntologyFileResponse, PutOntologyFileRequest>(
      `/agentic/ontologies/${encodeURIComponent(ontologyId)}/file`,
      {
        json: request,
        signal: options.signal,
      },
    );
  }

  uploadFile(
    ontologyId: string,
    formData: FormData,
    options: { signal?: AbortSignal } = {},
  ): Promise<UploadOntologyFileResponse> {
    return this.http.post<UploadOntologyFileResponse>(
      `/agentic/ontologies/${encodeURIComponent(ontologyId)}/upload`,
      {
        body: formData,
        signal: options.signal,
      },
    );
  }

  importZip(formData: FormData, options: { signal?: AbortSignal } = {}): Promise<ImportOntologyZipResponse> {
    return this.http.post<ImportOntologyZipResponse>('/agentic/ontologies/import-zip', {
      body: formData,
      signal: options.signal,
    });
  }

  delete(ontologyId: string, options: { signal?: AbortSignal } = {}): Promise<DeleteOntologyResponse> {
    return this.http.delete<DeleteOntologyResponse>(`/agentic/ontologies/${encodeURIComponent(ontologyId)}`, {
      signal: options.signal,
    });
  }
}
