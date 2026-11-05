<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

class ReportController extends Controller
{
    public function sales(Request $request)
    {
        $region = $request->input('region');
        // Planted: SQL injection through string concatenation.
        return DB::select("SELECT * FROM sales WHERE region = '" . $region);
    }

    public function preferences(Request $request)
    {
        // Planted: object injection from a client-controlled cookie.
        return unserialize($request->cookie('prefs'));
    }
}
