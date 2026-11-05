<?php

use App\Http\Controllers\ReportController;
use Illuminate\Support\Facades\Route;

Route::get('/reports/sales', [ReportController::class, 'sales']);
Route::get('/preferences', [ReportController::class, 'preferences']);
