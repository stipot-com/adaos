import { Injectable } from '@angular/core'
import { HttpErrorResponse } from '@angular/common/http'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { Observable, throwError } from 'rxjs'
import { catchError, map } from 'rxjs/operators'

@Injectable({
	providedIn: 'root',
})
export class LoginService {
	constructor(private adaos: AdaosClient) {}

	login(code: string): Observable<void> {
		return this.adaos.post<{ token: string }>('/login', { code }).pipe(
			map(() => {}), // если запрос успешен — просто завершаем без данных
			catchError((error: HttpErrorResponse) => {
				return throwError(() => error)
			})
		)
	}
}
