import HttpException from './models/http-exception.model';

export default async function (error: any, event: any) {
    const cause = error.cause ?? error;
    let statusCode: number;
    let body: any;

    if (cause instanceof HttpException) {
        statusCode = cause.errorCode;
        body = cause.message;
    } else if (error.statusCode && error.data) {
        statusCode = error.statusCode;
        body = error.data;
    } else {
        return;
    }

    setResponseStatus(event, statusCode);
    setResponseHeader(event, 'content-type', 'application/json');
    await send(event, JSON.stringify(body));
}
